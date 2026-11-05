#!/usr/bin/env python3
"""Build an LTO + PGO optimized simc command line binary (MSVC, Clang or GCC).

Runs the whole profile-guided optimization cycle with the CMake options from cmake/optimization.cmake:

  1. configure + build an instrumented binary        (-DSC_PGO=GENERATE -DSC_LTO=ON)
  2. train it: simulate a set of profiles from the latest tier to record an execution profile
  3. (Clang) merge the raw profiles with llvm-profdata
  4. configure + build the optimized binary           (-DSC_PGO=USE -DSC_LTO=ON)
  5. optionally (--verify) check that results are bit-identical to a plain Release build

The optimizations never change simulation results; --verify checks this by comparing the complete json
reports (fixed seed, single thread) of both binaries.

Examples:
  python tools/pgo_build.py                                   # build dir: build_pgo
  python tools/pgo_build.py --verify                          # also build build_release and compare
  python tools/pgo_build.py -- -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_EXE_LINKER_FLAGS=-fuse-ld=lld

On Windows run it from a Visual Studio developer command prompt (cl, link and the PGO runtime on PATH).
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAIN_CLASSES = ["Mage_Fire", "Warrior_Fury", "Priest_Shadow", "Death_Knight_Unholy", "Hunter_Marksmanship",
                 "Rogue_Outlaw", "Warlock_Destruction", "Druid_Feral", "Paladin_Retribution", "Shaman_Enhancement",
                 "Monk_Windwalker", "Demon_Hunter_Havoc"]
VERIFY_CLASSES = ["Mage_Fire", "Warrior_Fury", "Priest_Shadow", "Death_Knight_Unholy", "Rogue_Outlaw",
                  "Demon_Hunter_Havoc"]
# json report fields that legitimately differ between two runs / builds
IGNORED_JSON = {"timestamp", "build_time", "build_date", "git_revision", "git_branch", "elapsed_time_seconds",
                "elapsed_cpu_seconds", "init_time_seconds", "merge_time_seconds", "analyze_time_seconds",
                "report_time_seconds", "iteration_time_seconds"}


def log(msg):
    print(f"[pgo_build {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, **kw):
    log("  $ " + " ".join(map(str, cmd)))
    r = subprocess.run(list(map(str, cmd)), **kw)
    if r.returncode != 0:
        sys.exit(f"command failed with exit code {r.returncode}")
    return r


def cmake_build(build_dir, cmake_args, jobs):
    run(["cmake", "-S", ROOT, "-B", build_dir, "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_GUI=OFF",
         "-DBUILD_TESTING=OFF"] + cmake_args)
    run(["cmake", "--build", build_dir, "--target", "simc", "--config", "Release", "-j", str(jobs)])
    for name in ("simc.exe", "simc", "Release/simc.exe"):
        exe = Path(build_dir) / name
        if exe.is_file():
            return exe
    sys.exit(f"simc binary not found in {build_dir}")


def cmake_cache(build_dir):
    cache = {}
    for line in (Path(build_dir) / "CMakeCache.txt").read_text(errors="replace").splitlines():
        m = re.match(r"^([A-Za-z0-9_]+):[A-Z]+=(.*)$", line)
        if m:
            cache[m.group(1)] = m.group(2)
    return cache


def compiler_family(build_dir):
    """('msvc' | 'clang' | 'gcc', compiler path) from the CMake compiler identification."""
    cxx = cmake_cache(build_dir).get("CMAKE_CXX_COMPILER", "")
    ident = ""
    for f in glob.glob(str(Path(build_dir) / "CMakeFiles" / "*" / "CMakeCXXCompiler.cmake")):
        m = re.search(r'set\(CMAKE_CXX_COMPILER_ID "([^"]*)"\)', Path(f).read_text(errors="replace"))
        if m:
            ident = m.group(1)
    if ident == "MSVC":
        return "msvc", cxx
    if "Clang" in ident:
        return "clang", cxx
    if ident == "GNU":
        return "gcc", cxx
    sys.exit(f"unsupported compiler '{ident}' for PGO")


def latest_tier():
    """Most recent tier profile directory (e.g. profiles/MID2): highest tier number among tier-looking dirs."""
    tiers = [d for d in (ROOT / "profiles").iterdir() if d.is_dir() and list(d.glob("*_Mage_Fire.simc"))]
    if not tiers:
        sys.exit("no tier profile directory found under profiles/")

    def key(d):
        t = subprocess.run(["git", "log", "-1", "--format=%ct", "--", f"profiles/{d.name}"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
        m = re.match(r"[A-Za-z]+(\d+)", d.name)
        return int(t or 0), int(m.group(1)) if m else 0, d.name

    return max(tiers, key=key)


def profiles(tier, classes):
    out = []
    for cls in classes:
        p = tier / f"{tier.name}_{cls}.simc"
        if p.is_file():
            out.append(p)
    return out


def find_llvm_profdata(cxx):
    """llvm-profdata matching the clang in use (clang++-20 -> llvm-profdata-20), else any on PATH."""
    cxx_path = Path(cxx)
    m = re.search(r"-(\d+)$", cxx_path.stem)
    names = ([f"llvm-profdata-{m.group(1)}"] if m else []) + ["llvm-profdata"]
    for n in names:
        cand = cxx_path.parent / (n + cxx_path.suffix)
        if cand.is_file():
            return str(cand)
        found = shutil.which(n)
        if found:
            return found
    if sys.platform == "darwin":  # Apple clang ships it inside Xcode
        r = subprocess.run(["xcrun", "--find", "llvm-profdata"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    sys.exit("llvm-profdata not found (use --llvm-profdata)")


def train(exe, family, pgo_dir, tier, iterations, threads):
    env = dict(os.environ)
    if family == "clang":
        env["LLVM_PROFILE_FILE"] = str(Path(pgo_dir) / "simc-%p.profraw")
    for prof in profiles(tier, TRAIN_CLASSES):
        run([exe, prof, f"iterations={iterations}", f"threads={threads}", "target_error=0", "seed=1",
             "output=" + os.devnull], cwd=exe.parent, env=env, stdout=subprocess.DEVNULL)


def clean_profile_data(build_dir, family, pgo_dir):
    if Path(pgo_dir).exists():
        shutil.rmtree(pgo_dir)
    Path(pgo_dir).mkdir(parents=True)
    if family == "msvc":
        for f in glob.glob(str(Path(build_dir) / "**" / "*.pgc"), recursive=True):
            os.remove(f)


def sim_json(exe, prof, seed, iterations, out):
    run([exe, prof, "target_error=0", f"iterations={iterations}", "threads=1", f"seed={seed}", f"json2={out}",
         "output=" + os.devnull], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads(Path(out).read_text(encoding="utf-8"))


def json_diff(x, y, path="", acc=None):
    acc = [] if acc is None else acc
    if isinstance(x, dict) and isinstance(y, dict):
        for k in set(x) | set(y):
            if k not in IGNORED_JSON:
                json_diff(x.get(k), y.get(k), f"{path}/{k}", acc)
    elif isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
        for i, (u, v) in enumerate(zip(x, y)):
            json_diff(u, v, f"{path}[{i}]", acc)
    elif x != y:
        acc.append(path)
    return acc


def verify(reference, optimized, tier, work_dir, iterations):
    ok = True
    for i, prof in enumerate(profiles(tier, VERIFY_CLASSES)):
        a = sim_json(reference, prof, 11 + i, iterations, Path(work_dir) / "ref.json")
        b = sim_json(optimized, prof, 11 + i, iterations, Path(work_dir) / "opt.json")
        diff = json_diff(a, b)
        log(f"  {prof.stem:<32} {'identical' if not diff else 'DIFFERENT: ' + ', '.join(diff[:3])}")
        ok &= not diff
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", default=str(ROOT / "build_pgo"), help="build directory (default: build_pgo)")
    ap.add_argument("--tier", help="profile directory used for training, e.g. MID2 (default: latest tier)")
    ap.add_argument("--train-iterations", type=int, default=500, help="iterations per training profile")
    ap.add_argument("--train-threads", type=int, default=1, help="threads per training run")
    ap.add_argument("--no-lto", action="store_true", help="PGO only (MSVC always uses LTO with PGO)")
    ap.add_argument("--llvm-profdata", help="llvm-profdata executable (Clang only)")
    ap.add_argument("--verify", action="store_true",
                    help="also build a plain Release binary and check that results are bit-identical")
    ap.add_argument("--reference-dir", default=str(ROOT / "build_release"),
                    help="build directory of the plain Release reference used by --verify")
    ap.add_argument("--verify-iterations", type=int, default=200)
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("cmake_args", nargs="*", help="extra CMake arguments (after --)")
    a = ap.parse_args()

    build_dir = Path(a.build_dir)
    pgo_dir = build_dir / "pgo"
    tier = ROOT / "profiles" / a.tier if a.tier else latest_tier()
    lto = [] if a.no_lto else ["-DSC_LTO=ON"]
    log(f"training tier: {tier.name}")

    log("step 1/4: instrumented build")
    exe = cmake_build(build_dir, a.cmake_args + lto + ["-DSC_PGO=GENERATE"], a.jobs)
    family, cxx = compiler_family(build_dir)
    log(f"compiler: {family} ({cxx})")
    clean_profile_data(build_dir, family, pgo_dir)

    log("step 2/4: training")
    train(exe, family, pgo_dir, tier, a.train_iterations, a.train_threads)

    if family == "clang":
        log("step 3/4: merging profiles")
        tool = a.llvm_profdata or find_llvm_profdata(cxx)
        raw = sorted(glob.glob(str(pgo_dir / "*.profraw")))
        if not raw:
            sys.exit("training produced no .profraw file")
        run([tool, "merge", "-output=" + str(pgo_dir / "simc.profdata")] + raw)
    else:
        log("step 3/4: (profile data used in place)")

    log("step 4/4: optimized build")
    exe = cmake_build(build_dir, a.cmake_args + lto + ["-DSC_PGO=USE"], a.jobs)
    log(f"optimized binary: {exe}")

    if a.verify:
        log("verification: plain Release reference build")
        ref = cmake_build(Path(a.reference_dir), a.cmake_args + ["-DSC_LTO=OFF", "-DSC_PGO=OFF"], a.jobs)
        log("verification: comparing complete json reports")
        if not verify(ref, exe, tier, build_dir, a.verify_iterations):
            sys.exit("the optimized binary does NOT produce identical results")
        log("results are bit-identical")


if __name__ == "__main__":
    main()
