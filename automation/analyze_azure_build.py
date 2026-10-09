import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

ORGANIZATION = "https://dev.azure.com/abdulazeemmd3"
PROJECT = "p2"

ANALYZER_URL = "http://localhost:8080/api/analyze"

REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------
# RUN AZURE CLI COMMANDS
# --------------------------------------------------

def run_az_command(command):
    """Execute an Azure CLI command and return its output."""

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        error_message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Azure CLI command failed:\n{error_message}")

    return result.stdout


# --------------------------------------------------
# GET BUILD DETAILS
# --------------------------------------------------

def get_build(build_id):
    """Retrieve build details from Azure DevOps."""

    output = run_az_command([
        "az",
        "pipelines",
        "runs",
        "show",
        "--id",
        str(build_id),
        "--organization",
        ORGANIZATION,
        "--project",
        PROJECT,
        "--output",
        "json",
    ])

    return json.loads(output)


# --------------------------------------------------
# GET BUILD LOG METADATA
# --------------------------------------------------

def get_build_logs(build_id):
    """Retrieve log metadata for an Azure DevOps build."""

    output = run_az_command([
        "az",
        "devops",
        "invoke",
        "--area",
        "build",
        "--resource",
        "logs",
        "--route-parameters",
        f"project={PROJECT}",
        f"buildId={build_id}",
        "--api-version",
        "7.1",
        "--http-method",
        "GET",
        "--organization",
        ORGANIZATION,
        "--output",
        "json",
    ])

    return json.loads(output).get("value", [])


# --------------------------------------------------
# GET CONTENT OF ONE LOG
# --------------------------------------------------

def get_log_content(build_id, log_id):
    """Download the contents of a particular build log."""

    output = run_az_command([
        "az",
        "devops",
        "invoke",
        "--area",
        "build",
        "--resource",
        "logs",
        "--route-parameters",
        f"project={PROJECT}",
        f"buildId={build_id}",
        f"logId={log_id}",
        "--api-version",
        "7.1",
        "--http-method",
        "GET",
        "--organization",
        ORGANIZATION,
        "--output",
        "json",
    ])

    data = json.loads(output)

    lines = data.get("value", [])

    if isinstance(lines, list):
        return "\n".join(str(line) for line in lines)

    return str(lines)


# --------------------------------------------------
# SELECT THE ACTUAL FAILED TASK LOG
# --------------------------------------------------

def select_failed_log(build_id, logs):
    """
    Prefer a log that contains an actual task execution failure.
    Avoid selecting pipeline YAML or configuration logs.
    """

    log_contents = []

    # Download logs and look for the failed task's output.
    for log in logs:
        log_id = log.get("id")

        if log_id is None:
            continue

        content = get_log_content(build_id, log_id)

        if not content.strip():
            continue

        log_contents.append((log_id, content))

    # First preference: the known simulated failure task.
    for log_id, content in log_contents:
        if (
            "##[section]Starting: Simulate Build Failure" in content
            and "##[error]Bash exited with code '1'." in content
        ):
            print(f"Selected actual execution log ID: {log_id}")
            return content

    # Second preference: any task execution log containing an error.
    for log_id, content in log_contents:
        lower_content = content.lower()

        is_task_log = "##[section]starting:" in lower_content

        has_execution_error = (
            "##[error]" in lower_content
            or "exited with code" in lower_content
        )

        if is_task_log and has_execution_error:
            print(f"Selected failed task log ID: {log_id}")
            return content

    # Third preference: a log with a recognizable error message.
    for log_id, content in log_contents:
        lower_content = content.lower()

        has_error = (
            "error:" in lower_content
            or "exception" in lower_content
            or "failed" in lower_content
        )

        if has_error:
            print(f"Selected log containing an error: {log_id}")
            return content

    # Last fallback: combine logs so useful details are not discarded.
    print("No specific failed task log found. Combining available logs.")

    combined_logs = []

    for log_id, content in log_contents:
        combined_logs.append(
            f"===== BUILD LOG {log_id} =====\n{content}"
        )

    return "\n\n".join(combined_logs)


# --------------------------------------------------
# SEND LOGS TO THE AI ANALYZER
# --------------------------------------------------

def analyze_log(log_content):
    """Send the selected build log to the Spring Boot analyzer."""

    if not log_content.strip():
        raise RuntimeError("The build log is empty. Cannot analyze it.")

    response = requests.post(
        ANALYZER_URL,
        files={
            "file": (
                "azure-build.log",
                log_content.encode("utf-8"),
                "text/plain",
            )
        },
        timeout=360,
    )

    response.raise_for_status()

    return response.json()


# --------------------------------------------------
# SAVE JSON REPORT
# --------------------------------------------------

def save_report(build_id, build, analysis):
    """Save build information and AI analysis in a JSON file."""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    report_file = REPORT_DIR / (
        f"build_{build_id}_{timestamp}.json"
    )

    report = {
        "organization": ORGANIZATION,
        "project": PROJECT,
        "build_id": build_id,
        "build_number": build.get("buildNumber"),
        "build_status": build.get("status"),
        "build_result": build.get("result"),
        "analysis_timestamp": datetime.now().isoformat(),
        "analysis": analysis,
    }

    report_file.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    return report_file


# --------------------------------------------------
# MAIN WORKFLOW
# --------------------------------------------------

def main():
    if len(sys.argv) != 2:
        print(
            "Usage: python automation/analyze_azure_build.py BUILD_ID"
        )
        sys.exit(1)

    build_id = sys.argv[1]

    try:
        print(f"Fetching Azure DevOps build {build_id}...")

        # 1. Retrieve build details.
        build = get_build(build_id)

        status = str(build.get("status", "")).lower()
        result = str(build.get("result", "")).lower()

        print(f"Build status: {status}")
        print(f"Build result: {result}")

        # 2. Make sure the build has completed.
        if status != "completed":
            print("The build has not completed yet.")
            print("Please try again after the build finishes.")
            return

        # 3. Analyze only failed builds.
        if result != "failed":
            print("This build is not marked as failed.")
            print("No AI analysis performed.")
            return

        # 4. Retrieve the build logs.
        print("Retrieving build logs...")

        logs = get_build_logs(build_id)

        if not logs:
            print("No logs were returned for this build.")
            return

        print(f"Found {len(logs)} log entries.")

        # 5. Select the most relevant failure log.
        print("Searching for the actual failed task log...")

        selected_log = select_failed_log(build_id, logs)

        if not selected_log.strip():
            print("No usable build log content was found.")
            return

        # 6. Send logs to the AI analyzer.
        print("Sending logs to the AI analyzer...")

        analysis = analyze_log(selected_log)

        # 7. Save the analysis report.
        report_file = save_report(
            build_id,
            build,
            analysis,
        )

        # 8. Display the result.
        print("\n========== AI FAILURE ANALYSIS ==========")

        print(json.dumps(analysis, indent=2))

        print("\n========== BUILD INFORMATION ==========")
        print(f"Build ID: {build_id}")
        print(f"Build Number: {build.get('buildNumber')}")
        print(f"Build Result: {result}")

        print(f"\nReport saved to: {report_file}")

        print("\nAnalysis completed successfully.")

    except (
        RuntimeError,
        requests.RequestException,
        json.JSONDecodeError,
    ) as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

