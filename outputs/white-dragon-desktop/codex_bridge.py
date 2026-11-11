"""Read-only adapter for local Codex Desktop metadata and lifecycle events.

Never resumes threads, writes Codex databases, reads auth.json, or calls inference.
The local database/transcript formats are version-specific; expose failures explicitly.
"""
from __future__ import annotations
from dataclasses import dataclass,field,asdict
from pathlib import Path
import datetime,json,re,sqlite3,time,uuid
from urllib.parse import urlencode
from contextlib import closing

PRIORITY={'needs_input':0,'blocked':1,'ready':2,'running':3,'idle':4,'offline':5}
LABELS={'needs_input':'需要你确认','blocked':'任务失败','ready':'任务完成','running':'工作中','idle':'待机','offline':'Codex 未运行'}
QUESTION_TOOLS={'request_user_input','request_user_input_async'}

def chat_url(thread_id=None,prompt=None,cwd=None):
    if thread_id is not None:
        return 'codex://threads/'+str(uuid.UUID(thread_id))
    args={}
    if prompt is not None:args['prompt']=prompt
    if cwd is not None:args['path']=str(Path(cwd).resolve())
    return 'codex://threads/new'+('?' + urlencode(args) if args else '')

@dataclass
class Activity:
    id:str
    title:str
    status:str
    turn_id:str|None=None
    updated:float=0
    unread:bool=False
    source:str='local'
    progress:str=''

@dataclass
class Snapshot:
    activities:list[Activity]=field(default_factory=list)
    connected:bool=False
    error:str=''
    source:str='本机 Codex 事件'
    @property
    def primary(self):return self.activities[0] if self.activities else None
    def report(self):return {'connected':self.connected,'error':self.error,'source':self.source,'activities':[asdict(a) for a in self.activities]}

class RolloutCursor:
    """Track lifecycle and public progress, without retaining tool outputs or reasoning."""
    def __init__(self,path):
        self.path=Path(path);self.offset=None;self.partial=b''
        self.turn_id=None;self.lifecycle=None;self.pending={};self.review=False
        self.reset_progress()

    def reset_progress(self):
        self.plan=[]
        self.phase='正在分析任务'
        self.active_tools={}

    @property
    def progress(self):
        if self.plan:
            for index,step in enumerate(self.plan):
                if step['status']=='in_progress':
                    text=step['step']
                    if not re.search(r'[\u4e00-\u9fff]',text):text='处理当前步骤'
                    return f'第 {index+1}/{len(self.plan)} 步 · {text}'
            if all(step['status']=='completed' for step in self.plan):
                return f'已完成 {len(self.plan)}/{len(self.plan)} 步 · 正在整理结果'
        if self.active_tools:
            return next(reversed(self.active_tools.values()))
        return self.phase

    def update_plan(self,value):
        if not isinstance(value,list) or not value or len(value)>100:return
        plan=[]
        for item in value:
            if not isinstance(item,dict):return
            text=item.get('step');status=item.get('status')
            if not isinstance(text,str) or status not in {'pending','in_progress','completed'}:return
            plan.append({'step':' '.join(text.split())[:160],'status':status})
        self.plan=plan

    def public_message(self,content):
        if isinstance(content,list):
            content=' '.join(item.get('text','') for item in content if isinstance(item,dict) and item.get('type') in {'output_text','text'})
        if not isinstance(content,str):return
        text=re.sub(r'[*`#]','',content)
        text=re.split(r'[。！!?？\n]',text,1)[0]
        text=' '.join(text.split())[:160]
        if re.search(r'[\u4e00-\u9fff]',text):self.phase=text

    @staticmethod
    def tool_progress(name,arguments):
        name=name.split('.')[-1].split('__')[-1]
        if name in {'exec','exec_command','run','execute','js'}:
            text=arguments if isinstance(arguments,str) else json.dumps(arguments,ensure_ascii=False)
            if name=='exec':
                if re.search(r'\btools\.\w*apply_patch\s*\(',text):return '正在修改代码'
                if re.search(r'\btools\.\w*(?:web__run|search)\s*\(',text):return '正在搜索资料'
                if re.search(r'\btools\.\w*view_image\s*\(',text):return '正在查看图片'
            if re.search(r'\b(?:PyInstaller|cargo\s+build|dotnet\s+build|(?:npm|pnpm|yarn)\s+(?:run\s+)?build)\b',text,re.I):return '正在构建程序'
            if re.search(r'\b(?:pytest|ctest|dotnet\s+test)\b|\bpython[\d.]*(?:exe)?[^\r\n]*\btest_[\w.-]+\.py\b|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b',text,re.I):return '正在运行测试'
            if re.search(r'\b(?:Get-Content|rg|git\s+(?:diff|status)|read_text|readFile)\b',text):return '正在检查文件'
            return '正在执行工具'
        if name=='apply_patch':return '正在修改代码'
        if name in {'wait','write_stdin','sleep'}:return '正在等待执行结果'
        if 'search' in name or name=='web__run':return '正在搜索资料'
        if name=='view_image':return '正在查看图片'
        if 'imagegen' in name:return '正在生成图像'
        if name in {'update_plan','updatePlan'}:return '正在安排任务步骤'
        if name in {'request_permissions','request_user_input','request_user_input_async'}:return '等待你的确认'
        return '正在调用工具'

    def feed(self,x):
        payload=x.get('payload') or {};kind=x.get('type');event=payload.get('type')
        if kind=='event_msg':
            if self.turn_id is None and payload.get('turn_id'):self.turn_id=payload['turn_id']
            if event in {'task_started','turn_started'}:
                self.turn_id=payload.get('turn_id');self.lifecycle='running';self.pending.clear();self.review=False
                self.reset_progress()
            elif event in {'task_complete','task_completed','turn_completed','turn_aborted','task_interrupted','error','task_failed','turn_failed'}:
                if payload.get('turn_id') and self.turn_id and payload['turn_id']!=self.turn_id:return
                self.lifecycle=('completed' if event in {'task_complete','task_completed','turn_completed'} else
                                'interrupted' if event in {'turn_aborted','task_interrupted'} else 'failed')
                self.pending.clear();self.active_tools.clear()
            elif event in {'exec_approval_request','apply_patch_approval_request','approval_requested','request_user_input'}:
                if self.lifecycle not in {'completed','interrupted','failed'}:
                    self.pending[payload.get('call_id') or payload.get('request_id') or payload.get('id') or event]='approval'
            elif event in {'exec_approval_response','apply_patch_approval_response','approval_resolved'}:
                request_id=payload.get('call_id') or payload.get('request_id') or payload.get('id')
                if request_id:self.pending.pop(request_id,None)
                elif sum(v=='approval' for v in self.pending.values())==1:
                    self.pending={k:v for k,v in self.pending.items() if v!='approval'}
            elif event=='plan_update':self.update_plan(payload.get('plan'))
            elif event in {'item_started','item_completed'}:
                item=payload.get('item') or {}
                item_type=item.get('type')
                if item_type=='AgentMessage' and item.get('phase')=='commentary':self.public_message(item.get('content'))
                elif item_type=='Plan':self.update_plan(item.get('plan'))
                elif item_type in {'CommandExecution','FileChange','McpToolCall'}:
                    if event=='item_started':
                        tool={'CommandExecution':'exec_command','FileChange':'apply_patch','McpToolCall':item.get('tool','')}
                        self.active_tools[item.get('id')]=self.tool_progress(tool[item_type],item.get('arguments',item.get('command','')))
                    else:
                        self.active_tools.pop(item.get('id'),None)
                        self.phase='正在分析执行结果'
            return
        if kind!='response_item':return
        call_id=payload.get('call_id') or payload.get('id')
        if event=='message' and payload.get('role')=='user':
            # Ordinary messages may be steering; they do not answer every
            # outstanding asynchronous question. Resolve only explicit IDs.
            request_id=payload.get('call_id') or payload.get('request_id') or payload.get('in_reply_to')
            if request_id:self.pending.pop(request_id,None)
            elif len(self.pending)==1 and next(iter(self.pending.values())) in {'question','async_question'}:self.pending.clear()
        if event=='message' and payload.get('role')=='assistant' and payload.get('phase')=='commentary':
            self.public_message(payload.get('content'))
        if event in {'function_call','custom_tool_call'}:
            if self.lifecycle in {'completed','interrupted','failed'}:return
            name=str(payload.get('name','')).split('.')[-1]
            arguments=payload.get('arguments',payload.get('input',''))
            self.active_tools[call_id]=self.tool_progress(name,arguments)
            if name in {'update_plan','updatePlan'}:
                try:
                    value=json.loads(arguments) if isinstance(arguments,str) else arguments
                    if isinstance(value,dict):self.update_plan(value.get('plan'))
                except (ValueError,TypeError):pass
            if name in QUESTION_TOOLS:self.pending[call_id]='async_question' if name.endswith('_async') else 'question'
            elif name=='request_permissions':self.pending[call_id]='approval'
            elif name=='exec':
                # In code mode a permission request can be nested in the outer exec.
                code=str(payload.get('input',''))
                if re.search(r'\btools\.request_permissions\s*\(',code):self.pending[call_id]='approval'
        elif event in {'function_call_output','custom_tool_call_output'}:
            if call_id in self.active_tools:
                self.active_tools.pop(call_id)
                self.phase='正在分析执行结果'
            if call_id not in self.pending:return
            if self.pending[call_id]!='async_question':self.pending.pop(call_id,None)
            else:
                output=payload.get('output',{})
                if isinstance(output,str):
                    try:output=json.loads(output)
                    except ValueError:output={}
                if isinstance(output,dict) and ('answers' in output or output.get('accepted') is False or output.get('cancelled')):
                    self.pending.pop(call_id,None)

    def poll(self):
        size=self.path.stat().st_size
        reset=self.offset is None or size<self.offset
        with self.path.open('rb') as f:
            if reset:
                self.offset=max(0,size-1024*1024);self.partial=b'';self.pending.clear();self.turn_id=None;self.lifecycle=None
                self.reset_progress()
                f.seek(self.offset)
                if self.offset:f.readline();self.offset=f.tell()
            else:f.seek(self.offset)
            data=f.read(2*1024*1024);self.offset=f.tell()
        lines=(self.partial+data).split(b'\n');self.partial=lines.pop()
        if len(self.partial)>8*1024*1024:self.partial=b''
        for line in lines:
            try:self.feed(json.loads(line))
            except (ValueError,TypeError):continue

class CodexReader:
    def __init__(self,home=None):
        self.home=Path(home) if home is not None else Path.home()/'.codex'
        self.cursors={};self.acknowledged={}

    def connect(self,name):
        return sqlite3.connect((self.home/name).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)

    def acknowledge(self,thread_id,turn_id):self.acknowledged[thread_id]=turn_id

    def scan(self):
        try:
            with closing(self.connect('state_5.sqlite')) as meta:
                rows=meta.execute('''SELECT t.id,t.title,t.rollout_path,t.updated_at,t.source,
                    t.originator,r.first_unread_turn FROM threads t
                    LEFT JOIN thread_read_receipts r ON t.id=r.thread_id
                    WHERE t.archived=0 AND t.source='vscode'
                    ORDER BY COALESCE(t.recency_at_ms,t.updated_at_ms,t.updated_at*1000) DESC''').fetchall()
            activities=[]
            with closing(self.connect('thread_history_1.sqlite')) as history:
                for tid,title,path,updated,source,originator,first_unread in rows:
                    if originator not in (None,'Codex Desktop','codex_work_desktop'):continue
                    latest=history.execute('SELECT turn_id,status,started_at,completed_at FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1',(tid,)).fetchone()
                    turn,status,started,completed=latest if latest else (None,None,None,None)
                    unread=bool(first_unread) and self.acknowledged.get(tid)!=turn
                    if status=='inProgress':state='running'
                    elif status=='failed' and unread:state='blocked'
                    elif status=='completed' and unread:state='ready'
                    else:state='idle'
                    progress='正在分析任务' if state=='running' else ''
                    # SQLite may store a Windows extended-length path while home uses C:\.
                    if path.startswith('\\\\?\\UNC\\'):
                        path='\\\\'+path[8:]
                    elif path.startswith('\\\\?\\'):
                        path=path[4:]
                    event_path=Path(path)
                    # Follow only real local session files recorded by Codex.
                    roots=[(self.home/'sessions').resolve(),(self.home/'archived_sessions').resolve()]
                    if event_path.is_file() and any(event_path.resolve().is_relative_to(root) for root in roots):
                        cursor=self.cursors.setdefault(tid,RolloutCursor(event_path))
                        if cursor.path!=event_path:cursor=RolloutCursor(event_path);self.cursors[tid]=cursor
                        cursor.poll()
                        if cursor.turn_id==turn:
                            if cursor.lifecycle=='failed':state='blocked' if unread or status=='inProgress' else 'idle'
                            elif cursor.lifecycle=='interrupted':state='idle'
                            elif cursor.lifecycle=='completed' and state=='running':state='ready' if unread else 'idle'
                            if cursor.pending and state=='running':state='needs_input'
                            if state=='running':progress=cursor.progress
                    activities.append(Activity(tid,title or '未命名聊天',state,turn,float(completed or started or updated or 0),unread,progress=progress))
            # All active tasks survive the display history limit, including
            # older local chats. Only inactive history is capped.
            active=[a for a in activities if a.status in {'running','needs_input'}]
            history=[a for a in activities if a.status not in {'running','needs_input'}][:40]
            activities=active+history
            keep={tid for tid,*_ in rows};self.cursors={k:v for k,v in self.cursors.items() if k in keep}
            activities.sort(key=lambda a:(PRIORITY[a.status],-a.updated))
            return Snapshot(activities,True)
        except (OSError,sqlite3.Error,ValueError) as exc:
            # Schema changes are visible rather than silently displaying invented status.
            return Snapshot(error='本地状态接口不可用：'+type(exc).__name__)
