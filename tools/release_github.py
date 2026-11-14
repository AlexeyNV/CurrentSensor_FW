"""Audit the configured GitHub remote. Credentials never enter output or files."""
import argparse,hashlib,json,os,re,subprocess,sys,urllib.error,urllib.parse,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def remote():
    url=subprocess.check_output(['git','remote','get-url','origin'],cwd=ROOT,text=True).strip()
    match=re.fullmatch(r'https://github.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?',url)
    if not match:raise RuntimeError('Expected an explicit HTTPS GitHub origin; no repository guessed')
    return '/'.join(match.groups())


def credentials():
    env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never')
    result=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',
                          text=True,capture_output=True,env=env,timeout=15)
    if result.returncode:raise RuntimeError('Git credential helper unavailable; no credentials printed')
    values=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if not values.get('password'):raise RuntimeError('No GitHub credential available')
    return values['password']


def api(path,token,method='GET',data=None):
    headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
             'X-GitHub-Api-Version':'2022-11-28','User-Agent':'CurrentSensor-release'}
    if data is not None:headers['Content-Type']='application/json';data=json.dumps(data).encode()
    request=urllib.request.Request('https://api.github.com'+path,data=data,headers=headers,method=method)
    with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def release_checks():
    from tools.hil.flash_safe import inspect_elf
    out=ROOT/'release/v1.0.0'
    manifest=json.loads((out/'build_manifest.json').read_text(encoding='utf8'))
    validation=json.loads((out/'validation.json').read_text(encoding='utf8'))
    if not manifest.get('hardware_tested') or validation.get('host_tests')!={'gui':130,'cli':49,'firmware_c':16,'status':'PASS'}:
        raise RuntimeError('Hardware/regression qualification missing')
    for name,digest in manifest['artifacts'].items():
        if Path(name).name!=name or sha(out/name)!=digest:raise RuntimeError('Artifact hash mismatch')
    elf=inspect_elf(out/'CurrentSensor_FW_1.0.0.elf')
    if elf['sha256']!=validation['tested_elf_sha256'] or not validation['sector7']['verified_unchanged']:
        raise RuntimeError('ELF/sector7 qualification mismatch')
    if validation['native_60s']['errors'] or validation['native_60s']['payload_rate_hz']<10000:
        raise RuntimeError('Native qualification failed')
    for key in ('gui_short','gui_segmented'):
        if validation[key]['errors'] or not validation[key]['reconnected']:raise RuntimeError('GUI qualification failed')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT):raise RuntimeError('Working tree must be clean')
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    tag=subprocess.check_output(['git','rev-parse','v1.0.0^{commit}'],cwd=ROOT,text=True).strip()
    kind=subprocess.check_output(['git','cat-file','-t','refs/tags/v1.0.0'],cwd=ROOT,text=True).strip()
    if tag!=head or kind!='tag':raise RuntimeError('Expected annotated v1.0.0 on HEAD')
    return out,manifest,head


def assets(out,manifest,head):
    for source,target in [('README.md','README.md'),('RELEASE_NOTES_1.0.0.md','RELEASE_NOTES_1.0.0.md'),
                          ('LICENSE','LICENSE'),('docs/THIRD_PARTY_NOTICES.md','THIRD_PARTY_NOTICES.md'),
                          ('requirements-lock.txt','requirements-lock.txt')]:
        (out/target).write_bytes((ROOT/source).read_bytes())
    license_paths=['Drivers/CMSIS/LICENSE.txt','Drivers/CMSIS/Device/ST/STM32F4xx/LICENSE.txt',
                   'Drivers/STM32F4xx_HAL_Driver/LICENSE.txt','Middlewares/ST/STM32_USB_Device_Library/LICENSE.txt']
    toolchain=Path(subprocess.check_output(['where.exe','arm-none-eabi-gcc'],text=True).splitlines()[0]).parent.parent
    toolchain_license=toolchain/'share/doc/gcc-arm-none-eabi/license.txt'
    if not toolchain_license.is_file():raise RuntimeError('Toolchain license bundle unavailable')
    with zipfile.ZipFile(out/'THIRD_PARTY_LICENSES.zip','w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name in license_paths:archive.writestr(name,(ROOT/name).read_bytes())
        archive.writestr('GNU-toolchain-license.txt',toolchain_license.read_bytes())
        header=(ROOT/'STM32F411xx_FLASH.ld').read_text(encoding='utf8').split('/* Entry Point */')[0]
        archive.writestr('ST-BSD-source-header.txt',header)
    manifest['source_revision']=head
    (out/'build_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
    names=list(manifest['artifacts'])+['build_manifest.json','validation.json','README.md','RELEASE_NOTES_1.0.0.md',
            'LICENSE','THIRD_PARTY_NOTICES.md','THIRD_PARTY_LICENSES.zip','requirements-lock.txt']
    (out/'SHA256SUMS').write_text(''.join(f'{sha(out/name)}  {name}\n' for name in names),encoding='ascii')
    return [out/name for name in names+['SHA256SUMS']]


def publish(repo,token,make_public):
    out,manifest,head=release_checks()
    remote_tag=api('/repos/'+repo+'/git/ref/tags/v1.0.0',token)
    local_tag=subprocess.check_output(['git','rev-parse','refs/tags/v1.0.0'],cwd=ROOT,text=True).strip()
    if remote_tag['object']['sha']!=local_tag:raise RuntimeError('Remote tag does not match local annotated tag')
    try:api('/repos/'+repo+'/releases/tags/v1.0.0',token)
    except urllib.error.HTTPError as error:
        if error.code!=404:raise
    else:raise RuntimeError('Release v1.0.0 already exists; not overwritten')
    files=assets(out,manifest,head)
    body=(ROOT/'RELEASE_NOTES_1.0.0.md').read_text(encoding='utf8')
    release=api('/repos/'+repo+'/releases',token,'POST',dict(tag_name='v1.0.0',target_commitish=head,
        name='CurrentSensor v1.0.0',body=body,draft=True,prerelease=False))
    state=dict(repository=repo,commit=head,tag='v1.0.0',release_id=release['id'],draft=True,assets=[])
    state_file=out/'publication.json'
    state_file.write_text(json.dumps(state,indent=2),encoding='utf8')
    upload=release['upload_url'].split('{')[0]
    if urllib.parse.urlparse(upload).hostname!='uploads.github.com':raise RuntimeError('Unexpected GitHub upload host')
    for path in files:
        request=urllib.request.Request(upload+'?name='+urllib.parse.quote(path.name),data=path.read_bytes(),
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/octet-stream',
                     'Accept':'application/vnd.github+json','User-Agent':'CurrentSensor-release'},method='POST')
        with urllib.request.urlopen(request,timeout=60) as response:asset=json.load(response)
        expected='sha256:'+sha(path)
        if asset.get('digest')!=expected or asset['size']!=path.stat().st_size:
            raise RuntimeError('Uploaded asset digest/size unverified; release remains draft')
        state['assets'].append(dict(name=path.name,sha256=expected[7:],size=asset['size']))
        state_file.write_text(json.dumps(state,indent=2),encoding='utf8')
        print('Verified upload:',path.name,flush=True)
    release=api('/repos/'+repo+'/releases/'+str(release['id']),token,'PATCH',{'draft':False})
    if make_public:api('/repos/'+repo,token,'PATCH',{'private':False})
    info=api('/repos/'+repo,token)
    if info['private']:raise RuntimeError('Repository remains private; public release not complete')
    # Independently verify that the release is visible without credentials.
    request=urllib.request.Request('https://api.github.com/repos/'+repo+'/releases/tags/v1.0.0',
        headers={'Accept':'application/vnd.github+json','User-Agent':'CurrentSensor-release'})
    with urllib.request.urlopen(request,timeout=30) as response:public=json.load(response)
    if public['draft'] or len(public['assets'])!=len(files):raise RuntimeError('Public release verification failed')
    state.update(draft=False,public_verified=True,repository_url=info['html_url'],release_url=public['html_url'])
    state_file.write_text(json.dumps(state,indent=2)+'\n',encoding='utf8')
    print(json.dumps(state,indent=2))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--publish',action='store_true')
    parser.add_argument('--make-public',action='store_true');args=parser.parse_args()
    token=credentials();repo=remote()
    user=api('/user',token);result=api('/repos/'+repo,token)
    audit=dict(repository=result['full_name'],url=result['html_url'],login=user['login'],
               permissions=result.get('permissions'),visibility=result.get('visibility'))
    dest=ROOT/'results/release_100';dest.mkdir(parents=True,exist_ok=True)
    (dest/'github_audit.json').write_text(json.dumps(audit,indent=2),encoding='utf8')
    print(json.dumps(audit,indent=2))
    if args.publish:
        if not (result.get('permissions') or {}).get('push'):raise RuntimeError('No push permission')
        if args.make_public and not result['permissions'].get('admin'):raise RuntimeError('No admin permission')
        publish(repo,token,args.make_public)


if __name__=='__main__':main()
