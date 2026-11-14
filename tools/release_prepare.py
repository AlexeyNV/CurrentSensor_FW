"""Build fixed-date release artifacts. Does not flash, tag or publish."""
import hashlib,json,os,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.hil.flash_safe import inspect_elf


def main():
    out=ROOT/'release/v1.0.0';out.mkdir(parents=True,exist_ok=True)
    if (out/'build_manifest.json').exists():raise RuntimeError('Release artifacts already exist; do not overwrite tested binaries')
    epoch=int(datetime(2026,11,14,tzinfo=timezone.utc).timestamp())
    env=dict(os.environ,SOURCE_DATE_EPOCH=str(epoch))
    r=subprocess.run(['make','-j4','BUILD_DIR=build/release_1_0_0'],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (out/'build.log').write_bytes(r.stdout)
    if r.returncode:raise RuntimeError('Release build failed; see build.log')
    elf=out/'CurrentSensor_FW_1.0.0.elf'
    subprocess.run(['arm-none-eabi-objcopy','--strip-debug',str(ROOT/'build/release_1_0_0/CurrentSensor_FW.elf'),str(elf)],check=True)
    for suffix,format in (('bin','binary'),('hex','ihex')):
        subprocess.run(['arm-none-eabi-objcopy','-O',format,str(elf),str(elf.with_suffix('.'+suffix))],check=True)
    inspection=inspect_elf(elf);inspection['path']=elf.name
    manifest=dict(firmware_version='1.0.0',gui_version='1.0.0',source_date_epoch=epoch,
        build_command='make -j4 BUILD_DIR=build/release_1_0_0',elf_transform='arm-none-eabi-objcopy --strip-debug',
        compiler=subprocess.check_output(['arm-none-eabi-gcc','--version'],text=True).splitlines()[0],
        elf=inspection,hardware_tested=False)
    manifest['artifacts']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('CurrentSensor_FW_1.0.0.*')}
    (out/'build_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
    (out/'SHA256SUMS').write_text(''.join(f'{v}  {k}\n' for k,v in manifest['artifacts'].items()),encoding='ascii')
    print(json.dumps(manifest,indent=2))


if __name__=='__main__':main()
