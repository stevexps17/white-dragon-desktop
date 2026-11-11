"""Build the distributable ZIP and checksums from an already-built runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

REPOSITORY = 'stevexps17/white-dragon-desktop'


def package(root, runtime, destination, version):
    app = root / 'outputs/white-dragon-desktop'
    manifest = json.loads((app / 'assets/manifest.json').read_text(encoding='utf-8'))
    files = {}
    for path in runtime.rglob('*'):
        if path.is_file():
            files['WhiteDragon/' + path.relative_to(runtime).as_posix()] = path
    assets = {'manifest.json', 'portrait.png'}
    for state in manifest['states'].values():
        assets.update(state.get('chunks', []))
    for name in assets:
        path = (app / 'assets' / name).resolve()
        if not path.is_relative_to((app / 'assets').resolve()):
            raise ValueError('Asset outside directory')
        files['assets/' + name] = path
    for path in app.glob('*.py'):
        files[path.name] = path
    for name in ['CHANGELOG.md', 'README.md']:
        if (root / name).is_file():
            files[name] = root / name
    if 'WhiteDragon/WhiteDragon.exe' not in files:
        raise ValueError('Missing built executable')
    release = {
        'schema': 1, 'product': 'white-dragon-desktop', 'repo': REPOSITORY,
        'version': version,
        'files': {name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for name, path in sorted(files.items())},
    }
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f'WhiteDragon-v{version}-windows-x64.zip'
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            archive.write(path, name)
        archive.writestr('release.json', json.dumps(release, ensure_ascii=False, indent=2))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(
        checksum + '  ' + output.name + '\n', encoding='ascii')
    page = f'https://github.com/{REPOSITORY}/releases/tag/v{version}'
    notes = (root / 'CHANGELOG.md').read_text(encoding='utf-8')
    metadata = {
        'tag_name': 'v' + version, 'html_url': page, 'body': notes,
        'draft': False, 'prerelease': False,
        'assets': [{'name': name, 'state': 'uploaded',
                    'browser_download_url': f'https://github.com/{REPOSITORY}/releases/download/v{version}/{name}'}
                   for name in [output.name, output.name + '.sha256']],
    }
    (destination / 'update.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(output)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    package(args.root.resolve(), args.runtime.resolve(), args.output.resolve(), args.version)
