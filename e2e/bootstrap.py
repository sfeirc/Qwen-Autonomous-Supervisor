from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def run(argv: list[str], cwd: Path) -> None:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "command failed: " + " ".join(argv))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a disposable QAS E2E repository")
    parser.add_argument("destination")
    parser.add_argument("--github-repository", help="existing empty owner/repository to push")
    parser.add_argument("--bot-login", required=True)
    parser.add_argument("--maintainer-login", required=True)
    args = parser.parse_args()
    destination = Path(args.destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit(f"refusing non-empty destination: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).parent / "fixture"
    shutil.copytree(
        fixture,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", "*.pyo"),
    )
    replacements = {
        "replace-with-e2e-bot": args.bot_login,
        "replace-with-maintainer": args.maintainer_login,
    }
    for path in destination.rglob("*"):
        if path.is_file() and (
            path.suffix in {".md", ".toml", ".yml", ".yaml", ".py"} or path.name == "CODEOWNERS"
        ):
            text = path.read_text(encoding="utf-8")
            for old, new in replacements.items():
                text = text.replace(old, new)
            path.write_text(text, encoding="utf-8")
    run(["git", "init", "-b", "main"], destination)
    run(["git", "config", "user.name", "QAS E2E Bootstrap"], destination)
    run(["git", "config", "user.email", "qas-e2e@example.invalid"], destination)
    run(["git", "add", "."], destination)
    run(["git", "commit", "-m", "chore: bootstrap disposable E2E product"], destination)
    if args.github_repository:
        run(
            ["git", "remote", "add", "origin", f"https://github.com/{args.github_repository}.git"],
            destination,
        )
        run(["git", "push", "-u", "origin", "main"], destination)
        run(
            [
                "gh",
                "label",
                "create",
                "source:user",
                "--repo",
                args.github_repository,
                "--color",
                "1D76DB",
                "--force",
            ],
            destination,
        )
        marker = "<!-- qas-operation:e2e-seed-health -->"
        run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                args.github_repository,
                "--title",
                "Implement GET /health semantics",
                "--body",
                marker + '\nReturn status code 200 with `{"status": "ok"}` and add tests.',
                "--label",
                "source:user",
            ],
            destination,
        )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
