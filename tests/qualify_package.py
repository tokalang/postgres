#!/usr/bin/env python3
"""Qualify official/postgres deterministic suites and a locked package consumer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


PACKAGE = Path(__file__).resolve().parents[1]


class QualificationError(RuntimeError):
    pass


def run(argv: list[str], *, cwd: Path,
        env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    if result.returncode != 0:
        raise QualificationError(
            "command failed (%d): %s\nstdout:\n%s\nstderr:\n%s"
            % (result.returncode, " ".join(argv), result.stdout, result.stderr)
        )
    return result


def resolve_toolchain(env: dict[str, str]) -> tuple[Path, Path, Path, Path, Path]:
    root_is_set = "TOKA_ROOT" in env
    explicit_keys = ("TOKA", "TOKAC", "TOKA_LIB")
    explicit_set = [key for key in explicit_keys if key in env]
    if root_is_set and explicit_set:
        raise QualificationError(
            "set either TOKA_ROOT or TOKA/TOKAC/TOKA_LIB, not both"
        )
    if root_is_set:
        if not env["TOKA_ROOT"].strip():
            raise QualificationError("TOKA_ROOT must not be empty")
        root = Path(env["TOKA_ROOT"]).expanduser().resolve()
        toka = root / "build" / "bin" / "toka"
        tokac = root / "build" / "bin" / "tokac"
        library = root / "lib"
        runtime = library / "sys" / "toka_rt.o"
        if not runtime.is_file():
            runtime = root / "build" / "lib" / "sys" / "toka_rt.o"
        build_driver = root / "tools" / "scripts" / "toka_build.py"
    else:
        if len(explicit_set) != len(explicit_keys):
            missing = ", ".join(key for key in explicit_keys if key not in env)
            raise QualificationError(
                "set TOKA_ROOT or all of TOKA/TOKAC/TOKA_LIB"
                + (" (missing: " + missing + ")" if missing else "")
            )
        empty = [key for key in explicit_keys if not env[key].strip()]
        if empty:
            raise QualificationError(
                "toolchain variables must not be empty: " + ", ".join(empty)
            )
        toka = Path(env["TOKA"]).expanduser().resolve()
        tokac = Path(env["TOKAC"]).expanduser().resolve()
        library = Path(env["TOKA_LIB"]).expanduser().resolve()
        runtime = library / "sys" / "toka_rt.o"
        build_driver = library / "toolchain" / "toka_build.py"

    required_files = {
        "toka": toka,
        "tokac": tokac,
        "toka_rt.o": runtime,
        "toka_build.py": build_driver,
    }
    missing_files = [name for name, path in required_files.items() if not path.is_file()]
    if not library.is_dir():
        missing_files.append("TOKA_LIB")
    if missing_files:
        raise QualificationError(
            "incomplete Toka toolchain (missing: %s)" % ", ".join(missing_files)
        )
    return toka, tokac, library, runtime, build_driver


def make_sdk(work: Path, source_library: Path, runtime: Path,
             build_driver: Path) -> Path:
    library = work / "sdk" / "lib"
    shutil.copytree(
        source_library,
        library,
        ignore=shutil.ignore_patterns("*.pyc", "__pycache__"),
    )
    runtime_dir = library / "sys"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(runtime, runtime_dir / "toka_rt.o")
    toolchain = library / "toolchain"
    toolchain.mkdir(parents=True, exist_ok=True)
    shutil.copy2(build_driver, toolchain / "toka_build.py")
    return library


def write_consumer(project: Path, dependency: Path) -> None:
    (project / "src").mkdir(parents=True)
    (project / "package.tk").write_text(
        "pub const PACKAGE = (\n"
        '    name = "postgres_consumer",\n'
        '    version = "0.1.0",\n'
        "    dependencies = (\n"
        "        postgres = %s,\n"
        "    )\n"
        ")\n" % json.dumps(str(dependency)),
        encoding="utf-8",
    )
    (project / "build.tk").write_text(
        "import build::{Executable, run_build}\n\n"
        "fn main() -> i32 {\n"
        '    auto app# = Executable::make(c"postgres_consumer", c"src/main.tk")\n'
        "    return run_build(app)\n"
        "}\n",
        encoding="utf-8",
    )
    (project / "src" / "main.tk").write_text(
        "import official/postgres::{PostgresDecode, PostgresQueryLimits, decode_backend_one, startup_message}\n"
        "import std/vec::{Vec}\n\n"
        "fn main() -> i32 {\n"
        '    if startup_message("app", "notes").is_err() { return 1 }\n'
        "    if PostgresQueryLimits::new(1, 1, 1).is_err() { return 3 }\n"
        "    auto frame# = Vec<u8>::new()\n"
        "    frame#.push('Z' as u8)\n"
        "    frame#.push(0:u8)\n"
        "    frame#.push(0:u8)\n"
        "    frame#.push(0:u8)\n"
        "    frame#.push(5:u8)\n"
        "    frame#.push('I' as u8)\n"
        "    match decode_backend_one(frame).unwrap() {\n"
        "        auto PostgresDecode::Complete(_) => return 0\n"
        "        _ => return 2\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )


def main() -> int:
    host_env = dict(os.environ)
    toka, tokac, source_library, runtime, build_driver = resolve_toolchain(host_env)

    with tempfile.TemporaryDirectory(prefix="toka-postgres-package-") as temporary:
        work = Path(temporary)
        sdk = make_sdk(work, source_library, runtime, build_driver)
        base_env = dict(host_env)
        base_env.update({"TOKAC": str(tokac), "TOKA_LIB": str(sdk)})
        base_env.pop("TOKA_ROOT", None)
        base_env.pop("TOKA", None)
        base_env.pop("TOKA_OFFLINE", None)
        exec_env = dict(base_env)
        exec_env.pop("TOKA_LIB", None)
        dependency = work / "postgres"
        shutil.copytree(
            PACKAGE,
            dependency,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )

        include = ["-I", str(sdk), "-I", str(dependency / "lib")]
        deterministic_suites = (
            "protocol_v1",
            "client_v1",
            "query_v1",
            "extended_v1",
            "pool_v1",
            "pool_extended_v1",
        )
        for suite in deterministic_suites:
            program = work / suite
            run([str(tokac), *include,
                 str(dependency / "tests" / (suite + ".tk")),
                 "-o", str(program)], cwd=PACKAGE, env=base_env)
            run([str(program)], cwd=PACKAGE, env=exec_env)

        project = work / "consumer"
        write_consumer(project, dependency)

        run([str(toka), "fetch"], cwd=project, env=base_env)
        lock = project / "package.lock"
        locked = lock.read_bytes()
        if not locked.startswith(b"toka-lock-v1\n") or b"postgres" not in locked:
            raise QualificationError("Postgres consumer did not produce a v1 lock with postgres")

        offline_env = dict(base_env)
        offline_env["TOKA_OFFLINE"] = "1"
        run([str(toka), "fetch"], cwd=project, env=offline_env)
        if lock.read_bytes() != locked:
            raise QualificationError("offline Postgres fetch changed package.lock")
        run([str(toka), "build"], cwd=project, env=offline_env)
        program = project / "target" / "debug" / "postgres_consumer"
        if not program.is_file():
            raise QualificationError("toka build did not produce PostgreSQL consumer")
        run([str(program)], cwd=project, env=offline_env)

    print(json.dumps({
        "result": "pass",
        "schema": "toka.official-postgres-query-v1",
        "stages": {
            "locked_local_dependency": "pass",
            "offline_lock_replay": "pass",
            "public_import_build_run": "pass",
            "wire_codec": "pass",
            "tls_scram_startup": "pass",
            "serial_simple_query": "pass",
            "extended_prepared_query": "pass",
            "transaction_ownership": "pass",
            "bounded_connection_pool": "pass",
            "pooled_parameters_and_transactions": "pass",
        },
        "version": 1,
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, QualificationError, subprocess.TimeoutExpired) as error:
        print("FAIL: " + str(error))
        raise SystemExit(1)
