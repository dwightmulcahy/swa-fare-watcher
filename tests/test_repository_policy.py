import pathlib
import re


ROOT = pathlib.Path(__file__).parents[1]


def test_external_actions_are_pinned_to_commit_shas():
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        workflow = path.read_text(encoding="utf-8")
        for reference in re.findall(r"uses:\s+([^\s#]+)", workflow):
            if reference.startswith("./"):
                continue
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), (
                f"{path.name}: mutable action reference {reference}"
            )


def test_base_images_are_versioned_and_digest_pinned():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    images = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    assert images
    for image in images:
        if image == "scratch":  # empty built-in stage, nothing to pin
            continue
        assert re.fullmatch(r"[^:]+:[^@]+@sha256:[0-9a-f]{64}", image)


def test_ci_scans_the_built_image():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "aquasecurity/trivy-action@" in workflow
    assert "severity: CRITICAL,HIGH" in workflow
    assert 'exit-code: "1"' in workflow


def test_release_publishes_protected_semver_aliases():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "type=semver,pattern={{version}}" in workflow
    assert "type=semver,pattern={{major}}.{{minor}}" in workflow
    assert "type=semver,pattern={{major}}" in workflow
    assert "type=raw,value=latest" in workflow
    for alias in ("publish_major", "publish_minor", "is_latest"):
        assert workflow.count(f"needs.verify.outputs.{alias} == 'true'") == 1
    assert "scripts/generate_release_notes.py" in workflow


def test_workflows_do_not_set_info_zip_option_env_vars():
    # zip/unzip read ZIP, ZIPOPT, UNZIP and UNZIPOPT as default command-line options, so a
    # variable holding an archive name breaks every zip call in that step (v0.1.0 release).
    pattern = re.compile(r"^\s*(ZIP|ZIPOPT|UNZIP|UNZIPOPT)\s*:", re.M)
    for wf in (ROOT / ".github").rglob("*.yml"):
        assert not pattern.search(wf.read_text(encoding="utf-8")), f"{wf}: rename the env var"
