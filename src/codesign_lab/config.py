import hashlib,json,sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def load(path):
    text=Path(path).read_text()
    try: return json.loads(text)
    except json.JSONDecodeError:
        try: import yaml
        except ImportError: raise ValueError("Use JSON-compatible YAML with this pinned runtime; PyYAML is optional") from None
        return yaml.safe_load(text)
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def bootstrap():
    official=(ROOT/load(ROOT/'configs/toolchain.yaml')['official_root']).resolve()
    sys.path.insert(0,str(official))
    return official
def verify_official():
    official=bootstrap(); manifest=load(official/'isolation-manifest.json')
    for name,expected in manifest['files_sha256'].items():
        if digest(official/name)!=expected: raise ValueError('Official input changed: '+name)
    return manifest

def reference(path):
    p=Path(path).resolve()
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)
def resolve_reference(path):
    p=Path(path)
    return p if p.is_absolute() else ROOT/p

def workspace_output(path,category):
    p=Path(path).resolve()
    if p.parent==(ROOT/'workspace').resolve():p=ROOT/'workspace'/category/p.name
    p.parent.mkdir(parents=True,exist_ok=True)
    return p
