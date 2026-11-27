"""Scratch-only native regression runner. Run from clean pinned ShenYu checkout."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

BASE = "9a739f5bf1e29d9ccda68f277a7a51d5ccf2afd0"
PASSWORD = "shenyu_ci_fixture"
IMAGES = {
    "mysql": ("mysql:8.0@sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b",
              3306, "db/init/mysql/schema.sql", "MySQL", "root",
              ["MYSQL_ROOT_PASSWORD=" + PASSWORD, "MYSQL_ROOT_HOST=%"],
              ["mysqladmin", "ping", "-h127.0.0.1", "-uroot", "-p" + PASSWORD],
              ["mysql", "-uroot", "-p" + PASSWORD]),
    "pg": ("postgres:15@sha256:724292da1f2e50bdccfc3302ce75bbba7f4a6076701b588cc795fcac65683550",
           5432, "db/init/pg/create-table.sql", "PostgreSQL", "postgres",
           ["POSTGRES_PASSWORD=" + PASSWORD, "POSTGRES_DB=shenyu"],
           ["pg_isready", "-h127.0.0.1", "-Upostgres", "-dshenyu"],
           ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-Upostgres", "-dshenyu"]),
}
CASES = {
    "ApiMapperTest": ("testTagFilterPaginatesDistinctApis", "expected: <2> but was: <3>"),
    "MockRequestRecordMapperTest": ("testSelectByQueryOrdersByDateUpdatedAndId", "expected: <1> but was: <3>"),
}
MAPPERS = "shenyu-admin/src/test/java/org/apache/shenyu/admin/mapper/"
PKG = "org.apache.shenyu.admin.mapper."


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def verify(tree, evidence, product, dialect, phase, rc):
    assert (rc == 0) == (phase == "candidate"), f"Unexpected Maven exit {rc}; inspect logs"
    for name, (method, signature) in CASES.items():
        file = tree / f"shenyu-admin/target/surefire-reports/TEST-{PKG}{name}.xml"
        report = ET.parse(file).getroot()
        cases = report.findall("testcase")
        assert len(cases) == 1 and cases[0].attrib["name"] == method
        case = cases[0]
        assert case.find("error") is None and case.find("skipped") is None
        # Surefire may omit stdout for a passing testcase. The Maven log must record
        # exactly two JDBC assertions from the two selected methods in this context.
        failure = case.find("failure")
        if phase == "base":
            assert failure is not None and signature in (failure.text or "")
        else:
            assert failure is None
        shutil.copy(file, evidence / f"{dialect}-{phase}-{name}.xml")
    log = (evidence / f"{dialect}-{phase}-maven.log").read_text()
    assert log.count("Native Admin JDBC product: " + product) == len(CASES)


def main():
    dialect, tag, mock, evidence_arg = sys.argv[1:]
    root = Path.cwd()
    overlay = Path(__file__).resolve().parent / "native-db-profile.patch"
    patches = [Path(tag).resolve(), Path(mock).resolve()]
    evidence = Path(evidence_arg).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    assert output("git", "rev-parse", "HEAD") == BASE
    assert not output("git", "status", "--porcelain")
    image, port, schema, product, user, settings, ready, load = IMAGES[dialect]
    prefix = ["docker", "run", "-d", "-p", f"127.0.0.1::{port}"]
    for setting in settings:
        prefix.extend(["-e", setting])
    prefix.append(image)
    selector = ",".join(f"{name}#{case[0]}" for name, case in CASES.items())
    with tempfile.TemporaryDirectory() as temp:
        for phase in ["base", "candidate"]:
            tree = Path(temp) / phase
            run("git", "worktree", "add", "--detach", str(tree), BASE)
            container = None
            try:
                run("git", "apply", str(overlay), cwd=tree)
                for patch, test in zip(patches, CASES):
                    flags = ["--include=" + MAPPERS + test + ".java"] if phase == "base" else []
                    run("git", "apply", *flags, str(patch), cwd=tree)
                container = output(*prefix)
                for _ in range(60):
                    probe = subprocess.run(["docker", "exec", container, *ready],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if probe.returncode == 0:
                        break
                    time.sleep(2)
                else:
                    raise RuntimeError("Native database did not become ready")
                with (root / schema).open("rb") as sql, (evidence / f"{dialect}-{phase}-schema.log").open("wb") as log:
                    run("docker", "exec", "-i", container, *load, stdin=sql, stdout=log, stderr=subprocess.STDOUT)
                host_port = output("docker", "port", container, f"{port}/tcp").rsplit(":", 1)[1]
                url = (f"jdbc:mysql://127.0.0.1:{host_port}/shenyu"
                       "?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=Asia/Shanghai"
                       if dialect == "mysql" else f"jdbc:postgresql://127.0.0.1:{host_port}/shenyu")
                env = dict(os.environ, SHENYU_NATIVE_ADMIN_PROFILE=dialect, SPRING_PROFILES_ACTIVE=dialect,
                           SPRING_DATASOURCE_URL=url,
                           SPRING_DATASOURCE_USERNAME=user, SPRING_DATASOURCE_PASSWORD=PASSWORD)
                with (evidence / f"{dialect}-{phase}-maven.log").open("wb") as log:
                    rc = subprocess.run(["./mvnw", "-B", "-ntp", "-pl", "shenyu-admin", "-am", "test",
                                         "-Dtest=" + selector, "-Dsurefire.failIfNoSpecifiedTests=false",
                                         "-Dmaven.javadoc.skip=true", "-Dshenyu.database.init_enable=false",
                                         "-Dlogging.level.org.apache.shenyu.admin.mapper.MockRequestRecordMapper=DEBUG"],
                                        cwd=tree, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
                verify(tree, evidence, product, dialect, phase, rc)
                print(f"{dialect} {phase}: verified expected results on native {product}")
            finally:
                if container:
                    with (evidence / f"{dialect}-{phase}-database.log").open("wb") as log:
                        subprocess.run(["docker", "logs", container], stdout=log, stderr=subprocess.STDOUT)
                    run("docker", "rm", "-f", container, stdout=subprocess.DEVNULL)
                run("git", "worktree", "remove", "--force", str(tree))


if __name__ == "__main__":
    main()
