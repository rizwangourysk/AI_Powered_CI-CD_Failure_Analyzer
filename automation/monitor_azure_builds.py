import json
import subprocess
import sys
import time
from pathlib import Path

import requests


ORGANIZATION = "https://dev.azure.com/abdulazeemmd3"
PROJECT = "p2"

CHECK_INTERVAL = 60

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "automation" / "processed_builds.json"
ANALYZER_SCRIPT = BASE_DIR / "automation" / "analyze_azure_build.py"


def run_az_command(command):
    """Run Azure CLI and return its output."""

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(error)

    return result.stdout


def get_recent_builds():
    """Get recent completed builds from Azure DevOps."""

    output = run_az_command([
        "az",
        "pipelines",
        "runs",
        "list",
        "--organization",
        ORGANIZATION,
        "--project",
        PROJECT,
        "--top",
        "20",
        "--output",
        "json",
    ])

    return json.loads(output)


def load_processed_builds():
    """Load build IDs already processed by this monitor."""

    if not STATE_FILE.exists():
        return set()

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(str(build_id) for build_id in data)
    except (json.JSONDecodeError, OSError):
        print("Warning: Could not read state file; starting with empty state.")
        return set()


def save_processed_builds(processed_builds):
    """Persist processed build IDs."""

    STATE_FILE.write_text(
        json.dumps(sorted(processed_builds), indent=2),
        encoding="utf-8",
    )


def analyzer_is_healthy():
    """Check whether the Spring Boot analyzer is available."""

    try:
        response = requests.get(
            "http://localhost:8080/api/health",
            timeout=5,
        )

        return response.status_code == 200 and response.text.strip() == "ok"

    except requests.RequestException:
        return False


def analyze_failed_build(build_id):
    """Call the existing analyzer script for a failed build."""

    command = [
        sys.executable,
        str(ANALYZER_SCRIPT),
        str(build_id),
    ]

    result = subprocess.run(
        command,
        cwd=BASE_DIR,
        text=True,
        check=False,
    )

    return result.returncode == 0


def main():
    print("==========================================")
    print(" Azure DevOps AI Failure Monitor")
    print("==========================================")
    print(f"Organization: {ORGANIZATION}")
    print(f"Project: {PROJECT}")
    print(f"Check interval: {CHECK_INTERVAL} seconds")
    print(f"State file: {STATE_FILE}")
    print("Press Ctrl+C to stop monitoring.")
    print()

    if not analyzer_is_healthy():
        print("ERROR: AI analyzer is not responding on port 8080.")
        print("Start the Spring Boot application and try again.")
        sys.exit(1)

    processed_builds = load_processed_builds()

    print(f"Previously processed builds: {len(processed_builds)}")

    # Mark existing builds as known at startup.
    # This avoids unexpectedly analyzing old failures on first run.
    try:
        initial_builds = get_recent_builds()

        for build in initial_builds:
            build_id = build.get("id")

            if build_id is not None:
                processed_builds.add(str(build_id))

        save_processed_builds(processed_builds)

        print("Existing builds recorded.")
        print("Monitoring for new builds...")
        print()

    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR retrieving initial builds: {exc}")
        sys.exit(1)

    try:
        while True:
            try:
                builds = get_recent_builds()

                # Process older runs first when multiple new runs appear.
                builds.sort(key=lambda build: int(build.get("id", 0)))

                for build in builds:
                    build_id = build.get("id")

                    if build_id is None:
                        continue

                    build_id = str(build_id)

                    if build_id in processed_builds:
                        continue

                    status = str(build.get("status", "")).lower()
                    result = str(build.get("result", "")).lower()

                    if status != "completed":
                        continue

                    print()
                    print(f"New completed build detected: {build_id}")
                    print(f"Build result: {result}")

                    if result == "failed":
                        if not analyzer_is_healthy():
                            print(
                                "Analyzer unavailable. "
                                "Will retry this build on the next check."
                            )
                            continue

                        success = analyze_failed_build(build_id)

                        if not success:
                            print(
                                f"Analysis failed for build {build_id}. "
                                "Will retry later."
                            )
                            continue

                        print(f"AI analysis completed for build {build_id}.")

                    else:
                        print(
                            f"Build {build_id} did not fail. "
                            "No AI analysis needed."
                        )

                    # Mark only completed and successfully handled builds.
                    processed_builds.add(build_id)
                    save_processed_builds(processed_builds)

                print(
                    f"\nChecked Azure DevOps. "
                    f"Next check in {CHECK_INTERVAL} seconds."
                )

            except (RuntimeError, json.JSONDecodeError) as exc:
                print(f"Monitoring error: {exc}")

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\nMonitoring stopped by user.")
        save_processed_builds(processed_builds)


if __name__ == "__main__":
    main()
