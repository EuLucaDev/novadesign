#!/usr/bin/env python3
"""Check the converter after a batch of work: build, tests, clippy, the
corpus measurement with schema validation, the output diff against a base
revision, the README numbers, the render comparison, the privately held
documents and the privacy checks, with each piece of work done once.

Usage: python3 -I tools/check.py LABEL [--base REV] [--jobs N] [--config FILE]
                                [--schemas DIR] [--jing DIR] [--novadesign PATH]
                                [--render-seed DIR] [--no-render] [--show N]

Stages (docs/measurement.md, "Check runs"):

1. `cargo build --release`; the binary is copied to CACHE/bin/<hash>.
   `cargo test --release` (library, doc and fixture tests; tests/corpus.rs
   is left out, compare.py covers it) and `cargo clippy --all-targets` run
   in the background (clippy in its own target directory, CACHE/target-clippy).
2. compare.py --all --exclude own/ with schema validation, conversions kept
   in CACHE/out/<binary hash>/ (compare.py --cache) and validation results
   in CACHE/valid.tsv (--valid-cache): a package is converted once per
   binary, and validated and compared with its reference once per content
   (comparisons in CACHE/out/compare/). Tables and summary.json go to
   target/compare/ as with compare.py; --subtotal prefixes come from the
   configuration.
3. The outputs of the base revision (default origin/main): CACHE/revs.tsv
   maps commits to binaries. A commit without an entry is built as
   tools/diff_outputs.py builds it and converted into the cache, in the
   background while stage 2 runs. The diff compares exit status, stderr and
   package entries as diff_outputs.py does; the list goes to diff.tsv in
   the frozen folder.
4. tools/readme_numbers.py rewrites README.md from the summary of stage 2.
5. tools/render_compare.py with --cache and --reuse-results in
   CACHE/render: only triples whose conversion changed since the previous
   check run are measured again (and rendered with --novadesign).
6. The privately held documents of the configuration are converted and
   validated; `git grep` for the privacy pattern and a search for tracked
   binary files.
7. The binary, the tables, summary.json and the logs are copied to
   ~/.cache/indd-frozen-LABEL/.

CACHE is ~/.cache/indd-check (INDD_CHECK_CACHE overrides it). Temporary
files go to ~/.cache/indd-tmp. The configuration (--config, default
.thoughts/tools/check.json, not in the repository) is a JSON object with
any of: "subtotal" (list of path prefixes under corpus/ for compare.py
--subtotal), "private" (paths under corpus/ of privately held documents),
"privacy_pattern" (extended regular expression that no committed file may
match), "novadesign", "render_seed" (defaults of the options of the same
name), "schemas", "jing". Prints each stage's time and a summary; exit
status 1 if a check fails.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare  # noqa: E402
import diff_outputs  # noqa: E402

ROOT = compare.ROOT
CACHE = Path(os.environ.get("INDD_CHECK_CACHE", Path.home() / ".cache" / "indd-check"))
BINARY_SUFFIXES = r"\.(indd|indt|indb|idml|pdf|psd|ai|zip|otf|ttf|jpg|png|tif)$"

times = {}


class Stage:
    """Times a stage into `times`."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, *exc):
        times[self.name] = time.time() - self.start


def run(cmd, log=None, env=None):
    """Run a command in the repository; (exit status, output). With `log`,
    the output also goes to that file."""
    p = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, errors="replace")
    if log:
        Path(log).write_text(p.stdout)
    return p.returncode, p.stdout


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          text=True).stdout.strip()


def keep_binary(path):
    """Copy a converter binary to CACHE/bin/<hash>; its hash."""
    h = compare.file_sha256(path)[:16]
    dest = CACHE / "bin" / h
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        shutil.copyfile(path, tmp)
        tmp.chmod(0o755)
        tmp.replace(dest)
    return h


def revisions():
    """commit -> binary hash, from CACHE/revs.tsv."""
    try:
        lines = (CACHE / "revs.tsv").read_text().splitlines()
    except OSError:
        return {}
    return dict(line.split("\t") for line in lines if "\t" in line)


def record_revision(commit, h):
    if revisions().get(commit) != h:
        with open(CACHE / "revs.tsv", "a") as f:
            f.write(f"{commit}\t{h}\n")


def corpus_items():
    """(path under corpus/, INDD, reference IDML or None, cache key) of every
    file compare.py --all --exclude own/ converts, in its order."""
    seen = set()
    known = compare.known_digests()
    todo = compare.pairs(0, None, ["own/"], seen, known)
    others = compare.unpaired(None, ["own/"], seen, known)
    items = [(indd, None) for indd in others] + todo
    return [(str(indd.relative_to(ROOT / "corpus")), indd, idml,
             compare.cache_key(indd, compare.digest(indd, known))) for indd, idml in items]


def convert_all(binary, folder, items, jobs):
    """Records of every item converted by `binary` into `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(jobs) as pool:
        return list(pool.map(lambda it: compare.convert_cached(binary, it[1], it[3], folder,
                                                               False), items))


def base_outputs(commit, items, jobs, current):
    """(binary hash, records) of the base commit's conversions: cached ones
    are reused; a commit without a binary is built first."""
    h = revisions().get(commit)
    if not h or not (CACHE / "bin" / h).exists():
        h = keep_binary(diff_outputs.build(commit))
        record_revision(commit, h)
    if h == current:
        return h, None
    return h, convert_all(CACHE / "bin" / h, CACHE / "out" / h, items, jobs)


def diff(items, old, new, show, dest):
    """Compare the records of two conversions of `items` as diff_outputs.py
    does; print the counts and write diff.tsv to `dest`. Paths whose output
    or exit status differs."""
    results = []
    for (rel, _, _, _), a, b in zip(items, old, new):
        r = {"path": rel, "status": (a["exit"], b["exit"]),
             "warnings": a["stderr"].splitlines() != b["stderr"].splitlines(), "entries": []}
        if a["exit"] == 0 and b["exit"] == 0 and a["package"] != b["package"]:
            ea, eb = a["entries"], b["entries"]
            if ea is None or eb is None:
                r["entries"] = ["(unreadable package)"]
            else:
                if [n for n, _ in ea] != [n for n, _ in eb]:
                    r["entries"].append("(entry list)")
                da, db = dict(map(tuple, ea)), dict(map(tuple, eb))
                r["entries"] += [n for n in da if n in db and da[n] != db[n]]
        results.append(r)
    differ = [r for r in results if r["entries"]]
    status = [r for r in results if r["status"][0] != r["status"][1]]
    failed = [r for r in results if r["status"] != (0, 0) and r["status"][0] == r["status"][1]]
    warned = [r for r in results if r["warnings"]]
    kinds = compare.Counter(diff_outputs.kind(n) for r in differ for n in r["entries"])
    with open(dest / "diff.tsv", "w") as out:
        out.write("index\tpath\tstatus_old\tstatus_new\twarnings_differ\tentries\n")
        for i, r in enumerate(results):
            if r["entries"] or r["warnings"] or r["status"][0] != r["status"][1]:
                out.write(f"{i}\t{r['path']}\t{r['status'][0]}\t{r['status'][1]}\t"
                          f"{r['warnings']}\t{' '.join(r['entries'])}\n")
    print(f"files converted:            {len(results)}")
    print(f"outputs differ:             {len(differ)}")
    print(f"package entries differ:     {sum(kinds.values())}")
    print(f"failure status differs:     {len(status)}")
    print(f"warnings differ:            {len(warned)}")
    print(f"failed with both:           {len(failed)}")
    for k, n in kinds.most_common():
        print(f"  {n:6d}  {k}")
    for r in (differ + status + warned)[:show]:
        print(f"  [{results.index(r)}] {r['path']}: status {r['status']}, "
              f"warnings {'differ' if r['warnings'] else 'same'}, "
              f"entries {' '.join(r['entries'][:5])}")
    print(f"details: {dest / 'diff.tsv'}")
    return {"files": len(results), "differ": len(differ), "entries": sum(kinds.values()),
            "status": len(status), "warnings": len(warned), "failed": len(failed)}


def tests(dest):
    """Library, doc and fixture tests in release mode; (ok, text)."""
    passed = failed = 0
    ok = True
    for i, args in enumerate((["--lib", "--test", "fixtures"], ["--doc"])):
        code, out = run(["cargo", "test", "--release", "-q", *args], dest / f"tests{i}.txt")
        ok = ok and code == 0
        for m in re.finditer(r"test result: \w+\. (\d+) passed; (\d+) failed", out):
            passed, failed = passed + int(m[1]), failed + int(m[2])
        if code != 0:
            bad = [x for x in out.splitlines() if re.search(r"FAILED|panicked|^error", x)]
            print("tests:\n  " + "\n  ".join(bad[:10]))
    return ok, f"{passed} passed, {failed} failed" + ("" if ok else " (FAILED)")


def clippy(dest):
    env = dict(os.environ, CARGO_TARGET_DIR=str(CACHE / "target-clippy"))
    code, out = run(["cargo", "clippy", "--all-targets", "-q"], dest / "clippy.txt", env)
    lines = [x for x in out.splitlines() if x.strip()]
    if code == 0 and not lines:
        return True, "clean"
    print("clippy:\n  " + "\n  ".join(lines[:10]))
    return False, f"exit {code}, {len(lines)} lines of output"


def private(binary, folder, paths, vargs, vcache):
    """Convert and validate the privately held documents as
    .thoughts/tools/verify.sh does; (one text per document, whether any
    failed to convert or has schema errors)."""
    out = []
    bad = False
    for i, path in enumerate(paths, 1):
        indd = ROOT / "corpus" / path
        key = compare.cache_key(indd, compare.file_sha256(indd))
        rec = compare.convert_cached(binary, indd, key, folder, False)
        warnings = sum(1 for x in rec["stderr"].splitlines() if x.startswith("warning: "))
        if rec["exit"] != 0:
            bad = True
            out.append(f"{i}: conversion failed (exit {rec['exit']}): "
                       f"{compare.error_message(rec['stderr'])[:200]}")
            continue
        pkg = folder / f"{key}.idml"
        errs = compare.validate_cached([(pkg, rec["entries"])], vargs, vcache).get(str(pkg), [])
        bad = bad or bool(errs)
        out.append(f"{i}: warnings={warnings} schema errors={len(errs)}")
    return out, bad


def privacy(pattern):
    """Committed lines that match the privacy pattern, and tracked binary files."""
    hits = []
    if pattern:
        g = subprocess.run(["git", "-C", str(ROOT), "grep", "-n", "-i", "-E", pattern, "--",
                            ".", ":!corpus"], capture_output=True, text=True, errors="replace")
        hits = [x for x in g.stdout.splitlines() if not x.startswith("Binary")]
    tracked = [f for f in git("ls-files").splitlines() if re.search(BINARY_SUFFIXES, f, re.I)]
    return hits, tracked


def grep(text, pattern):
    m = re.search(pattern, text, re.M)
    return m[1] if m else "?"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("label", help="name of the run: ~/.cache/indd-frozen-LABEL/")
    ap.add_argument("--base", default="origin/main", help="revision to diff against")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--config", default=str(ROOT / ".thoughts" / "tools" / "check.json"))
    ap.add_argument("--schemas")
    ap.add_argument("--jing")
    ap.add_argument("--novadesign", help="NovaDesign command-line program")
    ap.add_argument("--render-seed", metavar="DIR",
                    help="earlier render_compare.py run whose renders the first check run reuses")
    ap.add_argument("--no-render", action="store_true", help="leave out the render comparison")
    ap.add_argument("--show", type=int, default=10, help="differing files to list")
    args = ap.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
    except OSError:
        config = {}
    schemas_home = Path.home() / ".cache" / "indd-schemas"
    schemas = args.schemas or config.get("schemas") or str(schemas_home / "schemas")
    jing = args.jing or config.get("jing") or str(schemas_home / "jing")
    novadesign = args.novadesign or config.get("novadesign") or os.environ.get("NOVADESIGN_CLI")
    seed = args.render_seed or config.get("render_seed")
    expand = (lambda p: os.path.expanduser(p) if p else p)
    schemas, jing, novadesign, seed = map(expand, (schemas, jing, novadesign, seed))
    os.environ["TMPDIR"] = str(Path.home() / ".cache" / "indd-tmp")
    Path(os.environ["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    dest = Path.home() / ".cache" / f"indd-frozen-{args.label}"
    dest.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    start = time.time()
    failed = []
    summary = []

    head = git("rev-parse", "HEAD")
    base = git("rev-parse", "--verify", args.base + "^{commit}")
    if not base:
        sys.exit(f"unknown revision {args.base}")
    print(f"== {args.label}: {git('log', '--oneline', '-1')}; "
          f"{git('status', '-sb').splitlines()[0]}")
    changed = git("status", "--porcelain", "-uall", "--", "src", "Cargo.toml", "Cargo.lock",
                  "build.rs")
    if changed:
        print("uncommitted changes to the source:\n  " + "\n  ".join(changed.splitlines()[:5]))

    with Stage("build"):
        code, out = run(["cargo", "build", "--release", "-q"])
        if code != 0:
            print(out[-3000:])
            sys.exit("build failed")
    h = keep_binary(ROOT / "target" / "release" / "indd")
    binary = CACHE / "bin" / h
    if not changed:
        record_revision(head, h)

    background = {}

    def later(name, fn, *a):
        """Run a stage in a thread; its result, or the exception it raised,
        goes to `background`."""
        def go():
            with Stage(name):
                try:
                    background[name] = fn(*a)
                except Exception as e:  # reported when the result is needed
                    background[name] = e
        t = threading.Thread(target=go)
        t.start()
        return t

    def result(name):
        r = background[name]
        if isinstance(r, Exception):
            sys.exit(f"{name} failed: {type(r).__name__}: {r}")
        return r

    items = corpus_items()
    jobs = [later("tests", tests, dest), later("clippy", clippy, dest),
            later("base outputs", base_outputs, base, items, args.jobs, h)]

    with Stage("compare"):
        cmd = [sys.executable, "-I", str(ROOT / "tools" / "compare.py"), "--all",
               "--exclude", "own/", "--schemas", schemas, "--jing", jing,
               "--bin", str(binary), "--cache", str(CACHE / "out"),
               "--valid-cache", str(CACHE / "valid.tsv"), "--jobs", str(args.jobs)]
        for prefix in config.get("subtotal", []):
            cmd += ["--subtotal", prefix]
        code, report = run(cmd, dest / "compare.txt")
        if code != 0:
            print(report[-3000:])
            sys.exit("compare.py failed")
    print("\n".join(x for x in report.splitlines()
                    if re.match(r"(conversion failures|schema |files without|story text|"
                                r"trustworthy pairs|without )", x)))
    st = json.loads((ROOT / "target" / "compare" / "summary.json").read_text())

    render = None
    if not args.no_render and novadesign:
        out = CACHE / "render"
        reuse = out if (out / "work").exists() or not seed else Path(seed)
        rcmd = [sys.executable, "-I", str(ROOT / "tools" / "render_compare.py"),
                "--novadesign", novadesign, "--bin", str(binary), "--cache", str(CACHE / "out"),
                "--reuse", str(reuse), "--reuse-results", str(out), "--out", str(out),
                "--jobs", str(args.jobs)]
        render = later("render", run, rcmd, dest / "render.txt")
    elif not args.no_render:
        print("render comparison left out: no --novadesign")

    jobs[2].join()
    with Stage("diff"):
        base_hash, base_recs = result("base outputs")
        print(f"\n== diff {args.base} ({base[:7]}, binary {base_hash}) .. working tree "
              f"(binary {h})")
        folder = CACHE / "out" / h
        recs = [compare.convert_cached(binary, it[1], it[3], folder, False) for it in items]
        d = diff(items, base_recs or recs, recs, args.show, dest)

    with Stage("readme"):
        code, out = run([sys.executable, "-I", str(ROOT / "tools" / "readme_numbers.py")])
        readme = out.strip().splitlines()[-1] if out.strip() else f"exit {code}"
        if code != 0:
            failed.append("README numbers")

    with Stage("private"):
        vargs = SimpleNamespace(schemas=schemas, jing=jing, jobs=args.jobs)
        vcache = compare.ValidationCache(CACHE / "valid.tsv", schemas, jing)
        docs, docs_bad = private(binary, CACHE / "out" / h, config.get("private", []), vargs,
                                 vcache)
    if docs_bad:
        failed.append("private documents")
    with Stage("privacy"):
        hits, tracked = privacy(config.get("privacy_pattern"))
    for x in hits[:5] + tracked[:5]:
        print(f"privacy: {x}")

    for t in jobs[:2]:
        t.join()
    rtext = None
    render_ok = False
    if render:
        render.join()
        code, rtext = result("render")
        render_ok = code == 0
        if not render_ok:
            print(rtext[-3000:])
            failed.append("render comparison")

    with Stage("freeze"):
        shutil.copyfile(binary, dest / "indd")
        (dest / "indd").chmod(0o755)
        for f in (ROOT / "target" / "compare").glob("*.tsv"):
            shutil.copyfile(f, dest / f.name)
        shutil.copyfile(ROOT / "target" / "compare" / "summary.json", dest / "summary.json")
        if render_ok:
            (dest / "render").mkdir(exist_ok=True)
            for f in list((CACHE / "render").glob("*.tsv")) + [CACHE / "render" / "summary.json"]:
                if f.exists():
                    shutil.copyfile(f, dest / "render" / f.name)

    # Summary.
    ok_tests, tests_text = result("tests")
    ok_clippy, clippy_text = result("clippy")
    paired, others = st["paired"], st["others"]
    tr, ev = st["trustworthy"], st["all_pairs"]

    def stories(x):
        s = x["stories"]
        return (f"{s['exact']:,}/{sum(s.values()):,} exact, {s['differ']} differ, "
                f"{s['missing']} missing")

    def cov(x):
        return (f"{100 * x['reproduced'] / x['values']:.2f} % "
                f"({x['reproduced']:,} of {x['values']:,})")
    if not ok_tests:
        failed.append("tests")
    if not ok_clippy:
        failed.append("clippy")
    if paired["failures"] or others["failures"]:
        failed.append("conversion failures")
    if paired["invalid"]:
        failed.append("schema validation (paired files)")
    if hits or tracked:
        failed.append("privacy")
    summary.append(f"tests: {tests_text}; clippy: {clippy_text}")
    summary.append(f"conversion failures: {len(paired['failures'])} of {paired['files']:,} paired, "
                   f"{len(others['failures'])} of {others['files']:,} other files; rejected "
                   f"{len(paired['rejected']) + len(others['rejected'])}")
    summary.append(f"schema failures: {paired['invalid']} paired, {others['invalid']} other files; "
                   f"{grep(report, r'^schema errors accepted.*: (\d+) files')} accepted (endnote "
                   f"markup); {grep(report, r'^schema validation: (.*)$')}")
    summary.append(f"story text: all pairs {stories(ev)}; trustworthy {stories(tr)}")
    summary.append(f"value coverage: trustworthy {cov(tr)}; all pairs {cov(ev)}")
    for prefix, sub in st.get("subtotals", {}).items():
        summary.append(f"without {prefix}: {sub['failures']} failures of {sub['files']} paired, "
                       f"{sub['invalid']} invalid; story text {stories(sub['all_pairs'])}; "
                       f"trustworthy {stories(sub['trustworthy'])}")
    summary.append(f"diff vs {args.base}: {d['differ']} outputs differ ({d['entries']} entries), "
                   f"failure status differs {d['status']}, warnings differ {d['warnings']}, "
                   f"failed with both {d['failed']}")
    summary.append(f"README: {readme}")
    if rtext is not None:
        summary.append("render: " + grep(rtext, r"^converter \(A vs B\): (.*?);") + "; "
                       + grep(rtext, r"^(verdicts and measurements reused: .*)$"))
    summary.append(f"private documents: {'; '.join(docs) or 'none configured'}")
    summary.append(f"privacy: {len(hits)} matches; tracked binary files: {len(tracked)}")
    summary.append(f"frozen: {dest}")
    times["total"] = time.time() - start

    print("\n== stage times (s; tests, clippy, base outputs and render run in the background)")
    for name, secs in times.items():
        print(f"  {name:14} {secs:7.1f}")
    print(f"\n== {args.label}: {'FAILED: ' + ', '.join(failed) if failed else 'ok'}")
    print("\n".join(summary))
    (dest / "check.txt").write_text("\n".join(summary) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
