#!/usr/bin/env python3
"""
subcruiser.py — Eagle Recon Manifest
------------------------------------------------
Subdomain-only installer + runner that:
  • Auto-installs (no prompts): subdomain enumerators (internally orchestrated)
  • Asks ONCE for the domain and runs phases STRICTLY ONE-AT-A-TIME:
      EAGLE WATCH (ALPHA) → EAGLE SWEEP (BRAVO) → EAGLE TRACE (CHARLIE) → optional VT
  • After EACH phase finishes, prints that phase’s normalized, deduped results to screen
    and writes a per-phase CSV immediately (no tool names leaked)
  • Aggregates all results, normalizes + de-dups, writes master TXT/CSV/HTML report

Outputs (in ./output/<domain>/):
  - sources/eagle_alpha.csv, eagle_bravo.csv, eagle_charlie.csv, eagle_vt.csv (if enabled)
  - eagle_manifest_raw.txt
  - eagle_manifest.txt
  - eagle_manifest.csv
  - eagle_manifest.html

Notes:
  - Linux (Ubuntu/Kali/Debian friendly). Run as root OR with sudo available.
  - VT requires VT_API_KEY env var; can be disabled by flag.
  - Tool names NEVER printed unless --internal-debug is set.
"""

from __future__ import annotations
import argparse
import csv
import html
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
from pathlib import Path
from typing import List, Optional, Tuple

GITHUB_API = "https://api.github.com"
TOOLS_DIR_DEFAULT = Path.home() / ".subzero-tools"
GO_VERSION = "1.22.5"


REPO_SUBFINDER   = "projectdiscovery/subfinder"
REPO_ASSETFINDER = "tomnomnom/assetfinder"
REPO_FINDOMAIN   = "Findomain/Findomain"

# -------------------------- util --------------------------
def have(prog: str) -> bool:
    return shutil.which(prog) is not None

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def is_writable_dir(p: Path) -> bool:
    try:
        ensure_dir(p)
        t = p / ".wtest"
        t.write_text("ok")
        t.unlink()
        return True
    except Exception:
        return False

def arch_pair() -> Tuple[str, str]:
    sysname = platform.system().lower()
    arch = platform.machine().lower()
    if sysname != "linux":
        print("[ERR] Linux required"); sys.exit(1)
    if arch in ("x86_64", "amd64"):
        return ("linux", "amd64")
    if arch in ("aarch64", "arm64"):
        return ("linux", "arm64")
    print(f"[ERR] unsupported arch: {arch}"); sys.exit(1)

def choose_prefix(prefix: Optional[str]) -> Path:
    if prefix:
        p = Path(os.path.expanduser(prefix)).resolve()
        if not is_writable_dir(p):
            print(f"[ERR] prefix not writable: {p}"); sys.exit(1)
        return p
    sys_bin = Path("/usr/local/bin")
    if is_writable_dir(sys_bin):
        return sys_bin
    user_bin = Path.home() / ".local" / "bin"
    if is_writable_dir(user_bin):
        return user_bin
    print("[ERR] no writable install prefix (/usr/local/bin or ~/.local/bin)"); sys.exit(1)

def sudo_prefix() -> List[str]:
    if os.geteuid() == 0:
        return []
    return ["sudo"] if have("sudo") else []

def run_stream(cmd: List[str], cwd: Optional[Path] = None, env: Optional[dict] = None,
               timeout: Optional[int] = None, quiet: bool = False) -> subprocess.CompletedProcess:
    """
    Stream stdout/stderr live unless quiet=True, with optional timeout to prevent silent hangs.
    """
    if not quiet:
        print(f"[CMD] {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
    )
    out_lines: List[str] = []
    start = time.time()

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            out_lines.append(line)
            if not quiet:
                print(line, end="")
            if timeout and (time.time() - start) > timeout:
                proc.kill()
                out_lines.append(f"\n[WRN] timeout after {timeout}s\n")
                break
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    return subprocess.CompletedProcess(cmd, proc.returncode or 0, "".join(out_lines), None)

def apt_install(pkgs: List[str], internal_debug: bool):
    if not have("apt-get"):
        return
    try:
        os.environ.setdefault("DEBIAN_FRONTEND", "noninteractive")
        # Time-bounded so it doesn't “hang forever”
        run_stream(sudo_prefix() + ["apt-get", "update"], timeout=180, quiet=not internal_debug)
        run_stream(sudo_prefix() + ["apt-get", "install", "-y"] + pkgs, timeout=600, quiet=not internal_debug)
    except Exception:
        pass

def download(url: str, dest: Path) -> bool:
    import urllib.request
    try:
        print(f"[DL ] {url}")
        with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return True
    except Exception as e:
        print(f"[ERR] download failed: {e}")
        return False

def gh_latest_asset(repo: str, contains: List[str]) -> Optional[Tuple[str, str]]:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{GITHUB_API}/repos/{repo}/releases/latest") as r:
            data = json.load(r)
        for a in data.get("assets", []):
            name = a.get("name", "")
            if all(tok in name for tok in contains):
                url = a.get("browser_download_url")
                if url:
                    return (name, url)
    except Exception as e:
        # Don't spam users; internal debug handled outside
        return None
    return None

def install_binary(src: Path, dest: Path):
    src.chmod(src.stat().st_mode | stat.S_IXUSR)
    ensure_dir(dest.parent)
    if dest.exists():
        try:
            dest.unlink()
        except Exception:
            pass
    shutil.move(str(src), dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXGRP | stat.S_IXOTH)

def git_clone_or_update(repo: str, into: Path, internal_debug: bool) -> Path:
    ensure_dir(into)
    name = repo.split("/")[-1]
    dst = into / name
    if dst.exists() and (dst / ".git").exists():
        if internal_debug:
            print(f"[git] updating {repo} …")
        run_stream(["git", "pull", "--rebase"], cwd=dst, timeout=120, quiet=not internal_debug)
        return dst
    if internal_debug:
        print(f"[git] cloning {repo} …")
    run_stream(["git", "clone", "--depth", "1", f"https://github.com/{repo}.git", str(dst)], timeout=180, quiet=not internal_debug)
    return dst

# --------------------- language toolchains ---------------------
def ensure_go(internal_debug: bool) -> bool:
    if have("go"):
        return True

    if internal_debug:
        print("[APT] installing golang …")
    apt_install(["golang-go"], internal_debug)

    if have("go"):
        return True

    if internal_debug:
        print("[DL ] installing official Go toolchain …")
    osn, arch = arch_pair()
    go_arch = "amd64" if arch == "amd64" else "arm64"
    url = f"https://go.dev/dl/go{GO_VERSION}.linux-{go_arch}.tar.gz"

    with tempfile.TemporaryDirectory() as td:
        tarf = Path(td) / f"go{GO_VERSION}.linux-{go_arch}.tar.gz"
        if not download(url, tarf):
            return False
        if Path("/usr/local/go").exists():
            run_stream(sudo_prefix() + ["rm", "-rf", "/usr/local/go"], timeout=120, quiet=not internal_debug)
        run_stream(sudo_prefix() + ["tar", "-C", "/usr/local", "-xzf", str(tarf)], timeout=180, quiet=not internal_debug)
        os.environ["PATH"] = f"/usr/local/go/bin:{os.environ.get('PATH','')}"
        if not Path("/usr/local/bin/go").exists():
            run_stream(sudo_prefix() + ["ln", "-sf", "/usr/local/go/bin/go", "/usr/local/bin/go"], timeout=30, quiet=not internal_debug)

    return have("go")

def ensure_rust(internal_debug: bool) -> bool:
    if have("cargo"):
        return True
    if internal_debug:
        print("[RST] installing rustup/cargo (user-local) …")
    try:
        run_stream(
            ["sh", "-c", "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"],
            timeout=300,
            quiet=not internal_debug,
        )
        cargo_home = Path.home() / ".cargo"
        binp = cargo_home / "bin"
        if binp.exists():
            os.environ["PATH"] = f"{str(binp)}:{os.environ.get('PATH','')}"
    except Exception:
        pass
    return have("cargo")

# -------------------------- installers (internal) --------------------------
def ensure_findomain(prefix: Path, tools_dir: Path, internal_debug: bool) -> Optional[Path]:
    if have("findomain"):
        if internal_debug:
            print(f"[OK ] findomain present: {shutil.which('findomain')}")
        return Path(shutil.which("findomain"))

    if have("apt-get"):
        if internal_debug:
            print("[APT] installing findomain …")
        apt_install(["findomain"], internal_debug)
        if have("findomain"):
            if internal_debug:
                print(f"[OK ] findomain installed via apt → {shutil.which('findomain')}")
            return Path(shutil.which("findomain"))

    # Try release asset (best-effort), else build from source
    repo = git_clone_or_update(REPO_FINDOMAIN, tools_dir, internal_debug)

    candidates = [["linux", "amd64"], ["linux", "x86_64"], ["linux-amd64"], ["linux64"], ["linux"]]
    asset = None
    for c in candidates:
        asset = gh_latest_asset(REPO_FINDOMAIN, c)
        if asset:
            break

    def _extract_install(arc: Path, name: str) -> Optional[Path]:
        binp = None
        try:
            lname = name.lower()
            if lname.endswith(".zip"):
                import zipfile
                with zipfile.ZipFile(arc, "r") as zf:
                    for n in zf.namelist():
                        base = os.path.basename(n).lower()
                        if base in ("findomain", "findomain-linux") or base.startswith("findomain"):
                            zf.extract(n, path=arc.parent)
                            binp = arc.parent / n
                            break
            elif lname.endswith(".tar.gz") or lname.endswith(".tgz"):
                import tarfile as tf
                with tf.open(arc, "r:gz") as t:
                    for m in t.getmembers():
                        base = os.path.basename(m.name).lower()
                        if base in ("findomain", "findomain-linux") or base.startswith("findomain"):
                            t.extract(m, path=arc.parent)
                            binp = arc.parent / m.name
                            break
            else:
                binp = arc
        except Exception:
            binp = None

        if binp and binp.exists():
            dest = prefix / "findomain"
            install_binary(binp, dest)
            cp = run_stream(["findomain", "-h"], quiet=True)
            if cp.returncode in (0, 2):
                if internal_debug:
                    print(f"[OK ] findomain installed → {dest}")
                return dest
        return None

    if asset:
        name, url = asset
        with tempfile.TemporaryDirectory() as td:
            arc = Path(td) / name
            if download(url, arc):
                ins = _extract_install(arc, name)
                if ins:
                    return ins

    if internal_debug:
        print("[CRG] building findomain from source with cargo …")
    if ensure_rust(internal_debug):
        rc = run_stream(["cargo", "build", "--release"], cwd=repo, timeout=600, quiet=not internal_debug)
        if rc.returncode == 0:
            binp = repo / "target" / "release" / "findomain"
            if binp.exists():
                dest = prefix / "findomain"
                install_binary(binp, dest)
                return dest

    return None

def ensure_subfinder(prefix: Path, tools_dir: Path, internal_debug: bool) -> Optional[Path]:
    if have("subfinder"):
        if internal_debug:
            print(f"[OK ] subfinder present: {shutil.which('subfinder')}")
        return Path(shutil.which("subfinder"))

    osn, arch = arch_pair()
    asset = gh_latest_asset(REPO_SUBFINDER, [osn, arch, ".tar.gz"]) or gh_latest_asset(REPO_SUBFINDER, [osn, arch, ".zip"])
    if asset:
        name, url = asset
        with tempfile.TemporaryDirectory() as td:
            arc = Path(td) / name
            if not download(url, arc):
                return None
            binp = None
            if name.endswith(".tar.gz"):
                with tarfile.open(arc, "r:gz") as tfz:
                    for m in tfz.getmembers():
                        if os.path.basename(m.name) == "subfinder":
                            tfz.extract(m, path=td)
                            binp = Path(td) / m.name
                            break
            else:
                import zipfile
                with zipfile.ZipFile(arc, "r") as zf:
                    for n in zf.namelist():
                        if os.path.basename(n) == "subfinder":
                            zf.extract(n, path=td)
                            binp = Path(td) / n
                            break
            if binp and binp.exists():
                dest = prefix / "subfinder"
                install_binary(binp, dest)
                return dest

    # Source build fallback
    if ensure_go(internal_debug):
        repo = git_clone_or_update(REPO_SUBFINDER, tools_dir, internal_debug)
        run_stream(["go", "build", "-o", str(prefix / "subfinder"), "./v2/cmd/subfinder"], cwd=repo, timeout=600, quiet=not internal_debug)
        if (prefix / "subfinder").exists():
            return prefix / "subfinder"

    return None

def ensure_assetfinder(prefix: Path, tools_dir: Path, internal_debug: bool) -> Optional[Path]:
    if have("assetfinder"):
        if internal_debug:
            print(f"[OK ] assetfinder present: {shutil.which('assetfinder')}")
        return Path(shutil.which("assetfinder"))

    repo = git_clone_or_update(REPO_ASSETFINDER, tools_dir, internal_debug)

    if not ensure_go(internal_debug):
        return None

    # Preferred: go install directly to prefix via GOBIN (most reliable)
    env = os.environ.copy()
    env.setdefault("GO111MODULE", "on")
    env.setdefault("GOPROXY", "https://proxy.golang.org,direct")
    env["GOBIN"] = str(prefix)

    run_stream(["go", "install", "github.com/tomnomnom/assetfinder@latest"], env=env, timeout=300, quiet=not internal_debug)
    dest = prefix / "assetfinder"
    if dest.exists():
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return dest

    # Fallback: local build
    run_stream(["go", "build", "-o", str(dest)], cwd=repo, timeout=300, quiet=not internal_debug)
    if dest.exists():
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return dest

    return None

# ----------------------- normalization/dedupe -----------------------
SUBDOM_RE_CACHE = {}

def valid_subdomain(candidate: str, root: str) -> bool:
    key = root
    if key not in SUBDOM_RE_CACHE:
        label = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
        SUBDOM_RE_CACHE[key] = re.compile(rf"^{label}(?:\.{label})*\.{re.escape(root)}$", re.IGNORECASE)
    return bool(SUBDOM_RE_CACHE[key].match(candidate))

def normalize(host: str, root: str) -> Optional[str]:
    h = host.strip().lower().rstrip(".")
    if not h:
        return None
    h = re.sub(r"^https?://", "", h).split("/")[0]
    if h.endswith("." + root) and h != root and valid_subdomain(h, root):
        return h
    return None

def collect_with(cmd: List[str], internal_debug: bool) -> List[str]:
    try:
        cp = run_stream(cmd, quiet=True)  # quiet: don't leak tool output formatting
        if internal_debug and cp.returncode != 0:
            print(f"[WRN] {' '.join(cmd)} exit {cp.returncode}")
        return [ln.strip() for ln in (cp.stdout or "").splitlines() if ln.strip()]
    except FileNotFoundError:
        if internal_debug:
            print(f"[WRN] not found: {cmd[0]}")
        return []

def show_phase_results(phase_label: str, domain: str, items: List[str]) -> List[str]:
    cleaned = sorted({s for s in (normalize(x, domain) for x in items) if s})
    print(f"\n[{phase_label}] Assets Identified: {len(cleaned)}")
    for s in cleaned:
        print(s)
    return cleaned

# ----------------------- VirusTotal (optional) -----------------------
def collect_virustotal(
    domain: str,
    pages: int = 5,
    per_page: int = 100,
    wait_ms: int = 1200,
    request_timeout: int = 20,
) -> List[str]:
    key = os.environ.get("VT_API_KEY")
    if not key:
        print("[VT ] VT_API_KEY not set — skipping VirusTotal")
        return []

    import urllib.request, urllib.parse

    per_page = max(1, min(100, int(per_page)))
    pages = max(1, int(pages))
    wait_s = max(0.0, float(wait_ms) / 1000.0)

    out: List[str] = []
    cursor: Optional[str] = None
    fetched_pages = 0
    base = f"https://www.virustotal.com/api/v3/domains/{domain}/subdomains"

    def build_url() -> str:
        u = f"{base}?limit={per_page}"
        if cursor:
            u += f"&cursor={urllib.parse.quote(cursor)}"
        return u

    headers = {"x-apikey": key, "Accept": "application/json", "User-Agent": "eagle-manifest/1.0"}

    while fetched_pages < pages:
        url = build_url()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=request_timeout) as r:
                body = r.read()
        except Exception as e:
            print(f"[VT ] request error: {e} — skipping remaining VT pages")
            break

        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            data = {}

        if isinstance(data, dict) and "error" in data:
            err = data.get("error", {})
            code = err.get("code") or err.get("status")
            msg = err.get("message") or str(err)
            if code in ("Unauthorized", 401):
                print("[VT ] 401 Unauthorized — check VT_API_KEY value"); break
            if code in ("Forbidden", 403):
                print("[VT ] 403 Forbidden — your VT plan may not include this endpoint"); break
            if code in ("Too Many Requests", 429):
                print("[VT ] 429 rate limited — slowing down further requests")
                time.sleep(wait_s * 2)
                continue
            print(f"[VT ] API error ({code}): {msg} — stopping VT step"); break

        items = data.get("data", []) if isinstance(data, dict) else []
        for d in items:
            sid = d.get("id") if isinstance(d, dict) else None
            if sid and valid_subdomain(sid, domain):
                out.append(sid)

        next_link = (data.get("links", {}) if isinstance(data, dict) else {}).get("next")
        if next_link and "cursor=" in next_link:
            cursor = next_link.split("cursor=")[-1]
            fetched_pages += 1
            time.sleep(wait_s)
        else:
            break

    cleaned = sorted(set(out))
    print(f"[VT ] Intel Pull: {len(cleaned)} assets")
    return cleaned

# ----------------------------- reporting -----------------------------
def write_csv(path: Path, rows: List[str]):
    ensure_dir(path.parent)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["subdomain"])
        for s in rows:
            w.writerow([s])

def write_master_outputs(domain: str, items: List[str], outdir: Path, phase_counts: List[Tuple[str, int]]):
    ensure_dir(outdir)

    rawf  = outdir / "eagle_manifest_raw.txt"
    uniqf = outdir / "eagle_manifest.txt"
    csvf  = outdir / "eagle_manifest.csv"
    htmlf = outdir / "eagle_manifest.html"

    with open(rawf, "w") as f:
        f.write("\n".join(items) + ("\n" if items else ""))

    dedup = sorted(set(items))
    with open(uniqf, "w") as f:
        f.write("\n".join(dedup) + ("\n" if dedup else ""))

    write_csv(csvf, dedup)

    phase_html = "\n".join(f"<tr><td>{html.escape(p)}</td><td>{c}</td></tr>" for p, c in phase_counts)
    rows_html = "\n".join(f"<tr><td>{html.escape(s)}</td></tr>" for s in dedup)

    html_doc = f"""
<!doctype html>
<html lang="en"><meta charset="utf-8"/>
<title>🦅 Eagle Recon Manifest — {html.escape(domain)}</title>
<style>
body{{font-family:system-ui,Arial,sans-serif;margin:2rem}}
table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ddd;padding:8px}} th{{background:#f4f4f4}}
h2{{margin-top:2rem}}
.badge{{display:inline-block;padding:.2rem .6rem;border:1px solid #ddd;border-radius:999px;margin-left:.4rem}}
</style>

<h1>🦅 Eagle Recon Manifest <span class="badge">Subdomain Discovery</span></h1>
<p>
Target: <b>{html.escape(domain)}</b><br>
Unique Assets: <b>{len(dedup)}</b><br>
</p>

<h2>Phase Contribution</h2>
<table><thead><tr><th>phase</th><th>assets</th></tr></thead><tbody>
{phase_html}
<tr><td><b>Final Manifest</b></td><td><b>{len(dedup)}</b></td></tr>
</tbody></table>

<h2>Final Manifest (Unique)</h2>
<table><thead><tr><th>subdomain</th></tr></thead><tbody>
{rows_html}
</tbody></table>
</html>
"""
    htmlf.write_text(textwrap.dedent(html_doc).strip())

    print(f"[OUT] {uniqf}")
    print(f"[OUT] {csvf}")
    print(f"[OUT] {htmlf}")

# ------------------------------ main ------------------------------
def main():
    ap = argparse.ArgumentParser(description="Eagle Recon Manifest — subdomain discovery (Linux)")
    ap.add_argument("--prefix", help="Install directory for binaries (/usr/local/bin or ~/.local/bin)")
    ap.add_argument("--tools-dir", default=str(TOOLS_DIR_DEFAULT), help="Where to clone repos")
    ap.add_argument("--dry-run", action="store_true", help="Install tools but do not run enumeration")
    ap.add_argument("--no-apt", action="store_true", help="Do not run apt-get update/install")
    ap.add_argument("--internal-debug", action="store_true", help="Show internal debug (may reveal tooling)")

    # VirusTotal tuning
    ap.add_argument("--vt-pages", type=int, default=5, help="Max VirusTotal pages to fetch (100 subs per page)")
    ap.add_argument("--vt-per-page", type=int, default=100, help="Results per page from VirusTotal (max 100)")
    ap.add_argument("--vt-disable", action="store_true", help="Skip VirusTotal enrichment even if VT_API_KEY is set")
    ap.add_argument("--vt-wait-ms", type=int, default=1200, help="Wait between VT requests in milliseconds")

    args = ap.parse_args()
    internal_debug = bool(args.internal_debug)

    prefix = choose_prefix(args.prefix)
    tools_dir = Path(os.path.expanduser(args.tools_dir)).resolve()
    ensure_dir(tools_dir)

    if str(prefix) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{prefix}:{os.environ.get('PATH','')}"

    print(f"[i  ] install prefix: {prefix}")
    print(f"[i  ] tools dir     : {tools_dir}")
    print("[EAGLE] Recon Engine Online")

    if not args.no_apt:
        apt_install(["git", "jq", "dnsutils", "curl", "python3-pip"], internal_debug)

    # Ensure tools (SEQUENTIAL, internal)
    t_findomain = ensure_findomain(prefix, tools_dir, internal_debug)
    t_subfinder = ensure_subfinder(prefix, tools_dir, internal_debug)
    t_assetfinder = ensure_assetfinder(prefix, tools_dir, internal_debug)

    print("\n[EAGLE] Platform Ready. Awaiting Target.\n")

    if args.dry_run:
        print("[DRY] Install-only mode complete. Exiting.")
        return

    domain = input("Enter the target root domain (e.g., example.com): ").strip().lower()
    if not domain or "." not in domain:
        print("[ERR] Please enter a valid domain like example.com"); sys.exit(1)

    outdir = Path("output") / domain
    sources_dir = outdir / "sources"
    ensure_dir(sources_dir)

    all_found: List[str] = []
    phase_counts: List[Tuple[str, int]] = []

    # PHASE ALPHA
    print("[ALPHA] Eagle Watch initiated …")
    if t_subfinder and have("subfinder"):
        raw = collect_with(["subfinder", "-silent", "-d", domain], internal_debug)
        cleaned = show_phase_results("ALPHA", domain, raw)
        write_csv(sources_dir / "eagle_alpha.csv", cleaned)
        all_found += cleaned
        phase_counts.append(("Eagle Watch (ALPHA)", len(cleaned)))
    else:
        print("[ALPHA] Eagle Watch unavailable — phase skipped")
        write_csv(sources_dir / "eagle_alpha.csv", [])
        phase_counts.append(("Eagle Watch (ALPHA)", 0))

    # PHASE BRAVO
    print("\n[BRAVO] Eagle Sweep initiated …")
    if t_findomain and have("findomain"):
        raw = collect_with(["findomain", "-q", "-t", domain], internal_debug)
        cleaned = show_phase_results("BRAVO", domain, raw)
        write_csv(sources_dir / "eagle_bravo.csv", cleaned)
        all_found += cleaned
        phase_counts.append(("Eagle Sweep (BRAVO)", len(cleaned)))
    else:
        print("[BRAVO] Eagle Sweep unavailable — phase skipped")
        write_csv(sources_dir / "eagle_bravo.csv", [])
        phase_counts.append(("Eagle Sweep (BRAVO)", 0))

    # PHASE CHARLIE
    print("\n[CHARLIE] Eagle Trace initiated …")
    if t_assetfinder and have("assetfinder"):
        raw = collect_with(["assetfinder", "--subs-only", domain], internal_debug)
        cleaned = show_phase_results("CHARLIE", domain, raw)
        write_csv(sources_dir / "eagle_charlie.csv", cleaned)
        all_found += cleaned
        phase_counts.append(("Eagle Trace (CHARLIE)", len(cleaned)))
    else:
        print("[CHARLIE] Eagle Trace unavailable — phase skipped")
        write_csv(sources_dir / "eagle_charlie.csv", [])
        phase_counts.append(("Eagle Trace (CHARLIE)", 0))

    # Optional Intel Pull (VirusTotal)
    vt_list: List[str] = []
    if not args.vt_disable:
        print("\n[INTEL] External Intel Pull initiated …")
        vt_list = collect_virustotal(
            domain,
            pages=args.vt_pages,
            per_page=args.vt_per_page,
            wait_ms=args.vt_wait_ms,
            request_timeout=20,
        )

    if vt_list:
        cleaned = show_phase_results("INTEL", domain, vt_list)
        write_csv(sources_dir / "eagle_vt.csv", cleaned)
        all_found += cleaned
        phase_counts.append(("External Intel Pull (VT)", len(cleaned)))
    else:
        # Only write the file if VT step was attempted/enabled.
        if not args.vt_disable:
            write_csv(sources_dir / "eagle_vt.csv", [])
            phase_counts.append(("External Intel Pull (VT)", 0))

    # Final normalization & strict validate
    normalized: List[str] = []
    for x in all_found:
        n = normalize(x, domain)
        if n and valid_subdomain(n, domain):
            normalized.append(n)

    write_master_outputs(domain, normalized, outdir, phase_counts)

    print("\n[EAGLE] Final Manifest Compiled")
    print(f"[EAGLE] Total Unique Assets: {len(set(normalized))}")

    print("\n===== EAGLE MANIFEST (UNIQUE) =====")
    for s in sorted(set(normalized)):
        print(s)

    print("\n[EAGLE] Mission complete.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[INT] aborted by user")
        sys.exit(130)
