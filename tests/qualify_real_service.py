#!/usr/bin/env python3
"""Qualify official/postgres against PostgreSQL 16 and 17 over TLS/SCRAM.

This is interoperability evidence, not a replacement for the deterministic
protocol fixtures. It fails closed: missing Docker, compiler, or TLS tooling
is recorded as ``not-run`` and exits 2, never as a passing package test.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid


PACKAGE = Path(__file__).resolve().parents[1]
POSTGRES_IMAGES = ("postgres:16-bookworm", "postgres:17-bookworm")
PASSWORD = "toka-password"


class QualificationFailure(RuntimeError):
    pass


def command(
    args: list[str],
    *,
    cwd: Path = PACKAGE,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise QualificationFailure(
            f"command failed ({completed.returncode}): {' '.join(args)}\n{completed.stdout}"
        )
    return completed.stdout.strip()


def cleanup_command(args: list[str]) -> None:
    subprocess.run(
        args,
        cwd=PACKAGE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def record(report_path: Path | None, payload: dict[str, Any]) -> None:
    if report_path is None:
        return
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def runner_prerequisite(tokac: Path, toka_lib: Path | None) -> tuple[bool, str]:
    if not tokac.is_file() or not os.access(tokac, os.X_OK):
        return False, f"Toka compiler is not executable: {tokac}"
    if toka_lib is None or not toka_lib.is_dir():
        return False, f"Toka library directory is unavailable: {toka_lib}"
    if not (toka_lib / "sys" / "toka_rt.o").is_file():
        return False, f"Toka runtime object is unavailable under: {toka_lib}"
    if shutil.which("docker") is None:
        return False, "Docker CLI is not installed"
    if shutil.which("openssl") is None:
        return False, "openssl is not installed"
    try:
        version = command(
            ["docker", "version", "--format", "{{.Server.Version}}"], timeout=15
        )
        return True, version
    except (OSError, QualificationFailure, subprocess.TimeoutExpired) as error:
        return False, f"Docker daemon is unavailable or cannot publish loopback ports: {error}"


def compile_fixture(tokac: Path, toka_lib: Path, output: Path) -> None:
    env = dict(os.environ)
    env["TOKA_LIB"] = str(toka_lib)
    command(
        [
            str(tokac),
            "-I",
            str(toka_lib),
            "-I",
            str(PACKAGE / "lib"),
            str(PACKAGE / "tests" / "real_service_v1.tk"),
            "-o",
            str(output),
        ],
        env=env,
        timeout=180,
    )


def published_port(container: str) -> int:
    published = command(["docker", "port", container, "5432/tcp"], timeout=20)
    line = published.splitlines()[0].strip()
    try:
        host, port = line.rsplit(":", 1)
        if host not in {"127.0.0.1", "[::1]"}:
            raise ValueError("not a loopback publication")
        return int(port)
    except ValueError as error:
        raise QualificationFailure(
            f"unexpected Docker loopback mapping {line!r}: {error}"
        ) from error


def wait_ready(args: list[str], *, attempts: int = 40) -> None:
    last_error = ""
    for _ in range(attempts):
        completed = subprocess.run(
            args,
            cwd=PACKAGE,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if completed.returncode == 0:
            return
        last_error = completed.stdout.strip()
        time.sleep(0.25)
    raise QualificationFailure(
        f"service did not become ready: {' '.join(args)}\n{last_error}"
    )


def wait_tls_ready(port: int, ca_cert: Path, *, attempts: int = 40) -> None:
    last_error = ""
    for _ in range(attempts):
        completed = subprocess.run(
            [
                "openssl",
                "s_client",
                "-starttls",
                "postgres",
                "-connect",
                f"127.0.0.1:{port}",
                "-servername",
                "localhost",
                "-CAfile",
                str(ca_cert),
                "-verify_return_error",
                "-brief",
            ],
            cwd=PACKAGE,
            input="",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=15,
        )
        if completed.returncode == 0:
            return
        last_error = completed.stdout.strip()
        time.sleep(0.25)
    raise QualificationFailure(
        f"PostgreSQL TLS endpoint did not become ready on 127.0.0.1:{port}\n{last_error}"
    )


def make_certificates(work: Path) -> tuple[Path, Path, Path]:
    ca_key = work / "ca.key"
    ca_cert = work / "ca.crt"
    server_key = work / "server.key"
    server_csr = work / "server.csr"
    server_cert = work / "server.crt"
    extensions = work / "server.ext"
    extensions.write_text(
        "[v3_req]\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    command(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
            "-days",
            "1",
            "-subj",
            "/CN=toka-postgres-real-service-ca",
        ]
    )
    command(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-keyout",
            str(server_key),
            "-out",
            str(server_csr),
            "-subj",
            "/CN=localhost",
        ]
    )
    command(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(server_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(server_cert),
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(extensions),
            "-extensions",
            "v3_req",
        ]
    )
    ca_key.chmod(0o600)
    ca_cert.chmod(0o644)
    server_cert.chmod(0o644)
    server_key.chmod(0o600)
    return ca_cert, server_cert, server_key


def docker_name(prefix: str) -> str:
    return f"toka-{prefix}-{uuid.uuid4().hex[:12]}"


def service_log(container: str) -> str:
    completed = subprocess.run(
        ["docker", "logs", container],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return completed.stdout[-8000:]


def build_image(tag: str, base_image: str, work: Path, cert: Path, key: Path) -> None:
    command(["docker", "pull", base_image], timeout=300)
    context = work / base_image.replace(":", "-")
    context.mkdir()
    shutil.copy2(cert, context / "server.crt")
    shutil.copy2(key, context / "server.key")
    (context / "Dockerfile").write_text(
        f"FROM {base_image}\n"
        "COPY server.crt /tls/server.crt\n"
        "COPY server.key /tls/server.key\n"
        "RUN chown postgres:postgres /tls/server.crt /tls/server.key "
        "&& chmod 600 /tls/server.key\n",
        encoding="utf-8",
    )
    command(["docker", "build", "-t", tag, str(context)], timeout=300)


def qualify_postgres(
    fixture: Path,
    base_image: str,
    *,
    work: Path,
    ca_cert: Path,
    server_cert: Path,
    server_key: Path,
) -> dict[str, str]:
    name = docker_name("postgres")
    image = docker_name("postgres-image")
    try:
        build_image(image, base_image, work, server_cert, server_key)
        command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-p",
                "127.0.0.1::5432",
                "-e",
                "POSTGRES_USER=toka",
                "-e",
                f"POSTGRES_PASSWORD={PASSWORD}",
                "-e",
                "POSTGRES_DB=toka",
                "-e",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
                image,
                "postgres",
                "-c",
                "ssl=on",
                "-c",
                "ssl_cert_file=/tls/server.crt",
                "-c",
                "ssl_key_file=/tls/server.key",
                "-c",
                "password_encryption=scram-sha-256",
            ],
            timeout=180,
        )
        wait_ready(["docker", "exec", name, "pg_isready", "-U", "toka", "-d", "toka"])
        port = published_port(name)
        wait_tls_ready(port, ca_cert)
        command([str(fixture), str(port), str(ca_cert)], timeout=90)
        server = command(
            [
                "docker",
                "exec",
                name,
                "psql",
                "-U",
                "toka",
                "-d",
                "toka",
                "-Atc",
                "SHOW server_version",
            ],
            timeout=20,
        )
        password_encryption = command(
            [
                "docker",
                "exec",
                name,
                "psql",
                "-U",
                "toka",
                "-d",
                "toka",
                "-Atc",
                "SHOW password_encryption",
            ],
            timeout=20,
        )
        if password_encryption != "scram-sha-256":
            raise QualificationFailure(
                f"unexpected password_encryption: {password_encryption!r}"
            )
        scram_secret = command(
            [
                "docker",
                "exec",
                name,
                "psql",
                "-U",
                "toka",
                "-d",
                "toka",
                "-Atc",
                "SELECT rolpassword LIKE 'SCRAM-SHA-256$%' "
                "FROM pg_authid WHERE rolname = 'toka'",
            ],
            timeout=20,
        )
        if scram_secret != "t":
            raise QualificationFailure("toka role does not use a SCRAM-SHA-256 secret")
        expected_major = base_image.removeprefix("postgres:").split("-", 1)[0]
        actual_major = server.split(".", 1)[0].split(" ", 1)[0]
        if actual_major != expected_major:
            raise QualificationFailure(
                f"unexpected server major for {base_image}: {server!r}"
            )
        image_id = command(
            ["docker", "image", "inspect", base_image, "--format", "{{.Id}}"],
            timeout=20,
        )
        return {
            "base_image_id": image_id,
            "server_version": server,
            "transport": "tls-private-ca-scram-sha-256",
        }
    except Exception as error:
        raise QualificationFailure(
            f"{base_image} failed: {error}\n{service_log(name)}"
        ) from error
    finally:
        cleanup_command(["docker", "rm", "-f", name])
        cleanup_command(["docker", "image", "rm", image])


def default_tokac() -> Path:
    configured = os.environ.get("TOKAC")
    if configured:
        return Path(configured)
    discovered = shutil.which("tokac")
    return Path(discovered) if discovered else Path("tokac")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokac", type=Path, default=default_tokac())
    parser.add_argument(
        "--toka-lib",
        type=Path,
        default=Path(os.environ["TOKA_LIB"]) if os.environ.get("TOKA_LIB") else None,
    )
    parser.add_argument("--report", type=Path, help="write a JSON evidence report")
    args = parser.parse_args()
    tokac = args.tokac.expanduser().resolve()
    toka_lib = args.toka_lib.expanduser().resolve() if args.toka_lib else None
    report_path = args.report.expanduser().resolve() if args.report else None
    report: dict[str, Any] = {
        "contract": "official-postgres-real-service-v1",
        "postgres_images": list(POSTGRES_IMAGES),
        "status": "not-run",
        "version": 1,
    }

    eligible, prerequisite = runner_prerequisite(tokac, toka_lib)
    if not eligible:
        report["reason"] = prerequisite
        record(report_path, report)
        print(f"NOT RUN: {prerequisite}", file=sys.stderr)
        return 2
    report["docker_server"] = prerequisite

    try:
        assert toka_lib is not None
        with tempfile.TemporaryDirectory(prefix="toka-postgres-real-") as temporary:
            work = Path(temporary)
            fixture = work / "postgres-real-service-v1"
            compile_fixture(tokac, toka_lib, fixture)
            ca_cert, server_cert, server_key = make_certificates(work)
            evidence: dict[str, dict[str, str]] = {}
            for image in POSTGRES_IMAGES:
                evidence[image] = qualify_postgres(
                    fixture,
                    image,
                    work=work,
                    ca_cert=ca_cert,
                    server_cert=server_cert,
                    server_key=server_key,
                )
            if tuple(evidence) != POSTGRES_IMAGES:
                raise QualificationFailure("incomplete PostgreSQL image matrix")
        report["postgres"] = evidence
        report["status"] = "passed"
        record(report_path, report)
        print("PASS: PostgreSQL 16/17 private-CA TLS/SCRAM compatibility matrix")
        return 0
    except Exception as error:
        report["status"] = "failed"
        report["reason"] = str(error)
        record(report_path, report)
        print(f"FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
