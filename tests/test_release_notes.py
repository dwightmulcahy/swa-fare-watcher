from unittest.mock import patch

from scripts.generate_release_notes import previous_tag, render


@patch("scripts.generate_release_notes.previous_tag", return_value="v1.1.0")
@patch(
    "scripts.generate_release_notes.commit_subjects",
    return_value=[
        ("abc1234", "feat(status): add dashboard"),
        ("def5678", "fix: prevent release race"),
        ("987abcd", "docs: explain Docker tags"),
    ],
)
def test_groups_conventional_commits(_subjects, _previous):
    notes = render("v1.2.0", "owner/app", "My App", "docker.io/owner/app")

    assert "# My App 1.2.0" in notes
    assert "## ✨ Features" in notes
    assert "**status:** add dashboard" in notes
    assert "https://github.com/owner/app/commit/abc1234" in notes
    assert "## 🐛 Bug fixes" in notes
    assert "docker pull docker.io/owner/app:1.2.0" in notes
    assert "compare/v1.1.0...v1.2.0" in notes


@patch("scripts.generate_release_notes.previous_tag", return_value=None)
@patch(
    "scripts.generate_release_notes.commit_subjects",
    return_value=[
        ("abc1234", "feat!: replace legacy configuration"),
        ("def5678", "fix(core): avoid crash", "BREAKING CHANGE: config path moved"),
    ],
)
def test_highlights_breaking_changes(_subjects, _previous):
    notes = render("v0.1.0", "owner/app", "My App", "owner/app")

    assert "## 💥 Breaking changes" in notes
    assert "replace legacy configuration" in notes
    assert "avoid crash" in notes


@patch(
    "scripts.generate_release_notes.git",
    return_value="v1.2.0\nv1.9.9\nv1.10.0\nv1.5.0-rc.1\nv2.0.0",
)
def test_previous_tag_uses_numerical_stable_semver(_git):
    assert previous_tag("v1.10.0") == "v1.9.9"


@patch("scripts.generate_release_notes.previous_tag", return_value="v1.0.0")
@patch(
    "scripts.generate_release_notes.commit_subjects",
    return_value=[
        ("abc1234", "security: prevent token leak"),
        ("def5678", "perf(engine): reduce allocations"),
        ("efg9012", "style: improve spacing"),
    ],
)
def test_additional_release_note_categories(_subjects, _previous):
    notes = render("v1.1.0", "owner/app", "My App", "owner/app")
    assert "## 🔒 Security" in notes
    assert "## ⚡ Performance" in notes
    assert "## 🎨 Styles" in notes
