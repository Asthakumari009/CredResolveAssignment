"""Same targets as the Makefile, for machines without make. Usage: python tasks.py <target> [args]

Like the Makefile, this only shells out to docker compose; no project Python runs on the host.
"""
import subprocess
import sys

RUN = ["docker", "compose", "run", "--rm", "app"]

TARGETS = {
    "up": [["docker", "compose", "up", "-d", "--build", "db"], ["docker", "compose", "build", "app"]],
    "down": [["docker", "compose", "down", "-v"]],
    "build": [["docker", "compose", "build", "app"]],
    "migrate": [RUN + ["python", "-m", "smartdialer.migrate"]],
    "test": [RUN + ["python", "-m", "pytest", "-q"]],
    "sim": [RUN + ["python", "-m", "sim.run_all"]],
    "loadtest": [RUN + ["python", "-m", "loadtest.run"]],
    "psql": [["docker", "compose", "exec", "db", "psql", "-U", "dialer", "dialer"]],
    "shell": [RUN + ["bash"]],
}


def main() -> int:
    if len(sys.argv) < 2:
        print("targets:", ", ".join([*TARGETS, "demo-<name>"]))
        return 2
    target, extra = sys.argv[1], sys.argv[2:]
    if target.startswith("demo-"):
        cmds = [RUN + ["python", "-m", "demos." + target[5:].replace("-", "_")]]
    elif target in TARGETS:
        cmds = TARGETS[target]
    else:
        print("unknown target:", target)
        return 2
    for i, cmd in enumerate(cmds):
        rc = subprocess.call(cmd + (extra if i == len(cmds) - 1 else []))
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
