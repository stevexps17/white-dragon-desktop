"""Stage continuous video frames without interpolation. --approve installs reviewed assets.
Requires local ffmpeg/ffprobe, Pillow and numpy. No account or network access.
"""
from pathlib import Path
from fractions import Fraction
import argparse,hashlib,json,math,shutil,subprocess,tempfile
import numpy as np
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[2]
LIVE=ROOT/'outputs/white-dragon-desktop/assets'
STAGING=ROOT/'work/white-dragon-desktop/video-staging'
STATES={'idle','waving','jumping','running-left','running-right','running','review','failed','waiting','success'}
W,H,COLS,CHUNK=576,672,4,16

def alpha_box(image):
    return image.getchannel('A').point(lambda v:255 if v>=16 else 0).getbbox()

def idle_reference():
    manifest=json.loads((LIVE/'manifest.json').read_text(encoding='utf-8'))
    entry=manifest['states']['idle']
    if entry['mode']!='continuous':return None
    with Image.open(LIVE/entry['chunks'][0]) as sheet:
        return sheet.crop((0,0,entry['width'],entry['height'])).convert('RGBA')

def premultiplied_mae(first,last):
    a=np.asarray(first).astype(float);b=np.asarray(last).astype(float)
    return round(float(np.abs(a[:,:,:3]*a[:,:,3:]/255-b[:,:,:3]*b[:,:,3:]/255).mean()),3)

def geometry_issues(height,reference_height):
    ratio=height/reference_height
    return [] if .95<=ratio<=1.05 else [f'Standing height {height}px differs from idle {reference_height}px; supply a higher-detail matching source.']

def green_key(a):
    rgb=a[:,:,:3].astype(np.float32)
    edge=max(2,min(a.shape[:2])//50)
    border=np.concatenate((rgb[:edge].reshape(-1,3),rgb[-edge:].reshape(-1,3),rgb[:,:edge].reshape(-1,3),rgb[:,-edge:].reshape(-1,3)))
    bg=np.median(border,axis=0);excess_bg=bg[1]-max(bg[0],bg[2])
    if excess_bg<15:raise ValueError('Border is not a clear green screen; inspect the source background')
    r,g,b=rgb.transpose(2,0,1);excess=g-np.maximum(r,b)
    alpha=np.clip(1-np.maximum(0,excess-3)/max(1,excess_bg-3),0,1)
    # Compression and lighting can vary even a nominally solid background.
    close=np.linalg.norm(rgb-bg,axis=2)<20
    alpha[close & (excess>10)]=0
    # Recover foreground edge colour instead of leaving desaturated green fringes.
    foreground=(rgb-bg[None,None,:]*(1-alpha[:,:,None]))/np.maximum(alpha[:,:,None],.03)
    foreground[:,:,1]=np.where(excess>0,np.minimum(foreground[:,:,1],np.maximum(foreground[:,:,0],foreground[:,:,2])),foreground[:,:,1])
    foreground[alpha==0]=0
    return np.dstack((foreground,alpha*a[:,:,3])).clip(0,255).astype('uint8')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('state',choices=sorted(STATES));p.add_argument('source',type=Path)
    p.add_argument('--green',action='store_true',help='Automatic green key; complex edges need manual cleanup')
    p.add_argument('--approve',action='store_true',help='Install only after inspecting staged contact sheet and report')
    p.add_argument('--reviewed-transition',action='store_true',help='Confirm idle/action/idle, eyes, wrist, linework and green edges were visually reviewed')
    p.add_argument('--label');p.add_argument('--start-frame',type=int,default=0)
    p.add_argument('--end-frame',type=int,help='Inclusive source frame index')
    args=p.parse_args();src=args.source.resolve()
    if args.start_frame<0 or (args.end_frame is not None and args.end_frame<args.start_frame):raise ValueError('Invalid frame interval')
    meta=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_streams','-of','json',str(src)]))['streams'][0]
    fps=float(Fraction(meta.get('avg_frame_rate') or meta['r_frame_rate']))
    if not 12<=fps<=120:raise ValueError('Unsupported source frame rate')
    nominal=float(Fraction(meta['r_frame_rate']))
    if abs(fps-nominal)>.1:raise ValueError('Variable frame rate input: convert to constant frame rate before import')
    sw,sh=meta['width'],meta['height'];size=sw*sh*4
    cmd=['ffmpeg','-v','error']
    if meta['codec_name']=='vp9':cmd+=['-c:v','libvpx-vp9'] # Native VP9 decoding may drop alpha.
    cmd+=['-i',str(src),'-an','-sn','-fps_mode','passthrough','-f','rawvideo','-pix_fmt','rgba','-']
    digest=hashlib.sha256(src.read_bytes()).hexdigest()
    reference=idle_reference() if args.state!='idle' else None
    reference_box=alpha_box(reference) if reference else None
    target_height=(reference_box[3]-reference_box[1]) if reference_box else round(H*.78)
    target_foot=reference_box[3] if reference_box else round(H*.95)
    tag=hashlib.sha256(f'{digest}:key-v3:{args.green}:{args.start_frame}:{args.end_frame}:{target_height}:{target_foot}'.encode()).hexdigest()[:12]
    dest=STAGING/(args.state+'-'+tag);dest.mkdir(parents=True,exist_ok=True)
    source_frames=dest/'source-frames';source_frames.mkdir(exist_ok=True)
    normalized_frames=dest/'frames';normalized_frames.mkdir(exist_ok=True)
    shutil.copy2(src,dest/('source'+src.suffix.lower()))
    with tempfile.TemporaryDirectory(prefix='pet-frames-',dir=STAGING) as temp:
        temp=Path(temp);process=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        n=0;source_n=0;first=last=None;transparent=0;union=None
        try:
            while True:
                data=bytearray()
                while len(data)<size:
                    block=process.stdout.read(size-len(data))
                    if not block:break
                    data.extend(block)
                if not data:break
                if len(data)!=size:raise ValueError('Truncated video frame')
                source_i=source_n;source_n+=1
                if source_i<args.start_frame or (args.end_frame is not None and source_i>args.end_frame):continue
                a=np.frombuffer(data,dtype='uint8').reshape(sh,sw,4).copy()
                Image.fromarray(a).save(source_frames/f'{source_i:05d}.png')
                if args.green:a=green_key(a)
                im=Image.fromarray(a)
                # VP9 alpha can contain almost invisible compression specks outside the character.
                box=alpha_box(im)
                if box is None:raise ValueError(f'Empty character at frame {n}')
                if n==0:first=im.copy();anchor=box
                union=box if union is None else (min(union[0],box[0]),min(union[1],box[1]),max(union[2],box[2]),max(union[3],box[3]))
                transparent+=int((a[:,:,3]<250).any());last=im.copy()
                im.save(temp/f'{n:05d}.png');n+=1
                if n>3600:raise ValueError('Clip exceeds 3600-frame limit')
            error=process.stderr.read().decode(errors='replace');code=process.wait()
            if code:raise RuntimeError(error)
        finally:
            if process.poll() is None:process.kill();process.wait()
        if n<2 or transparent!=n:raise ValueError('All frames must have alpha; use --green for green screen video')
        # One anchor for the entire clip. Never recenter each frame or upscale source detail.
        scale=min(1.,target_height/(anchor[3]-anchor[1]),W*.82/(anchor[2]-anchor[0]))
        ox=round(W/2-(anchor[0]+anchor[2])/2*scale);oy=round(target_foot-anchor[3]*scale)
        bounds=(union[0]*scale+ox,union[1]*scale+oy,union[2]*scale+ox,union[3]*scale+oy)
        if bounds[0]<0 or bounds[1]<0 or bounds[2]>W or bounds[3]>H:
            raise ValueError('Motion leaves normalized canvas. Correct source framing rather than crop the character.')
        names=[];samples=[];ends={};indices=set(np.linspace(0,n-1,min(12,n),dtype=int).tolist())
        for start in range(0,n,CHUNK):
            count=min(CHUNK,n-start);sheet=Image.new('RGBA',(W*COLS,H*math.ceil(count/COLS)))
            for local in range(count):
                i=start+local
                with Image.open(temp/f'{i:05d}.png') as im:
                    if scale<1:im=im.resize((round(sw*scale),round(sh*scale)),Image.Resampling.LANCZOS)
                    frame=Image.new('RGBA',(W,H));frame.paste(im,(ox,oy))
                    frame.save(normalized_frames/f'{i:05d}.png')
                    if i in {0,n-1}:ends[i]=frame.copy()
                    sheet.paste(frame,((local%COLS)*W,(local//COLS)*H))
                    if i in indices:samples.append((i,frame.copy()))
            name=f'{args.state}-{tag}-{start//CHUNK:03d}.webp';sheet.save(dest/name,lossless=True,method=3);names.append(name)
        contact=Image.new('RGB',(768,math.ceil(len(samples)/4)*240),(223,225,234));draw=ImageDraw.Draw(contact)
        for j,(i,frame) in enumerate(samples):
            frame.thumbnail((192,216),Image.Resampling.LANCZOS);x=j%4*192;y=j//4*240
            contact.paste(frame,(x,y+20),frame);draw.text((x+4,y+3),f'frame {i} / {i/fps:.2f}s',fill='black')
        contact.save(dest/'contact.png')
        actual_height=round((anchor[3]-anchor[1])*scale)
        issues=geometry_issues(actual_height,target_height) if reference is not None else []
        # Inspect idle -> first -> last -> idle on a common canvas before install.
        transition=[]
        if reference is not None:transition=[reference,ends[0],ends[n-1],reference]
        if transition:
            board=Image.new('RGB',(W*4,H),(225,222,235));draw=ImageDraw.Draw(board)
            for i,frame in enumerate(transition):
                board.paste(frame,(i*W,0),frame);draw.text((i*W+12,12),['idle','action first','action last','idle'][i],fill='black')
            board.save(dest/'transition.png')
        report={'source':str(src),'sha256':digest,'native_fps':fps,'source_size':[sw,sh],'source_frames':source_n,'selected_source_frames':[args.start_frame,args.start_frame+n-1],'frames':n,'interpolated_frames':0,
                'normalization_scale':scale,'standing_height_px':actual_height,'reference_height_px':target_height,
                'foot_anchor_px':target_foot,'source_anchor_bbox':list(anchor),'offset_px':[ox,oy],
                'independent_source_frames':str(source_frames),'independent_normalized_frames':str(normalized_frames),
                'blocking_issues':issues,'transition_reviewed':args.reviewed_transition,
                'first_to_idle_premultiplied_mae':premultiplied_mae(reference,ends[0]) if reference is not None else None,
                'last_to_idle_premultiplied_mae':premultiplied_mae(reference,ends[n-1]) if reference is not None else None,
                'first_last_premultiplied_mae':premultiplied_mae(first,last),'manual_review_required':['eyes blink together','stable face, hands, horns and tail','standing pose matches idle at both ends','no edge spill or clipping'],
                'method':'Continuous source frames only; fixed anchor; no optical flow or per-frame recentering.'}
        entry={'mode':'continuous','frames':n,'fps':fps,'duration':n/fps,'loop':args.state in {'idle','running-left','running-right','running','waiting','review'},
               'width':W,'height':H,'columns':COLS,'chunk_size':CHUNK,'chunks':names,'label':args.label or args.state,'source_sha256':digest,
               'standing_height_px':actual_height,'foot_anchor_px':target_foot,'source_anchor_bbox':list(anchor),'source_scale':scale}
        (dest/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (dest/'entry.json').write_text(json.dumps(entry,ensure_ascii=False,indent=2),encoding='utf-8')
        if args.approve:
            if issues:raise ValueError('Asset blocked: '+'; '.join(issues))
            if args.state!='idle' and not args.reviewed_transition:raise ValueError('Review transition.png and source frames, then pass --reviewed-transition to install')
            manifest=json.loads((LIVE/'manifest.json').read_text(encoding='utf-8'))
            folder=LIVE/'continuous'/dest.name;folder.mkdir(parents=True,exist_ok=True)
            for name in names:shutil.copy2(dest/name,folder/name)
            entry['chunks']=[(folder/name).relative_to(LIVE).as_posix() for name in names]
            manifest['states'][args.state]=entry
            pending=LIVE/'manifest.pending.json';pending.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');pending.replace(LIVE/'manifest.json')
        print(json.dumps({'staging':str(dest),'installed':args.approve,**report},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
