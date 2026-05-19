"""Project-label filter on Gmail history responses.

Pins the behavior that the Gmail webhook drops irrelevant pushes (UNREAD
toggles, CATEGORY_UPDATES, user-private labels) before doing any work. This
is the contract that keeps docker logs silent for non-project mailbox
activity.
"""

from __future__ import annotations

from gb_automations.routes.webhooks import (
    _collect_project_thread_ids,
    _history_has_any_changes,
)

PROJECT_LABELS = {"Label_35", "Label_38"}  # two project leaves


def _msg(thread_id: str, label_ids: list[str], msg_id: str = "m1") -> dict:
    return {"id": msg_id, "threadId": thread_id, "labelIds": label_ids}


def test_messagesAdded_with_project_label_keeps_thread():
    history = {
        "history": [
            {
                "messagesAdded": [
                    {"message": _msg("t1", ["INBOX", "Label_35"])},
                ],
            },
        ],
    }
    assert _collect_project_thread_ids(history, PROJECT_LABELS) == {"t1"}


def test_messagesAdded_without_project_label_drops_thread():
    history = {
        "history": [
            {
                "messagesAdded": [
                    {"message": _msg("t1", ["INBOX", "CATEGORY_UPDATES"])},
                ],
            },
        ],
    }
    assert _collect_project_thread_ids(history, PROJECT_LABELS) == set()


def test_labelsAdded_for_project_label_keeps_thread():
    # The "user just filed this email into a project" case.
    history = {
        "history": [
            {
                "labelsAdded": [
                    {
                        "message": _msg("t1", ["INBOX", "Label_38"]),
                        "labelIds": ["Label_38"],
                    },
                ],
            },
        ],
    }
    assert _collect_project_thread_ids(history, PROJECT_LABELS) == {"t1"}


def test_labelsAdded_for_non_project_label_drops_thread():
    # User marked the thread important — none of our business.
    history = {
        "history": [
            {
                "labelsAdded": [
                    {
                        "message": _msg("t1", ["INBOX"]),
                        "labelIds": ["IMPORTANT"],
                    },
                ],
            },
        ],
    }
    assert _collect_project_thread_ids(history, PROJECT_LABELS) == set()


def test_labelsRemoved_for_project_label_keeps_thread():
    # The "user moved this email from project A to project B" case — the
    # remove-side must still resync so Notion reconciles the dropped project.
    history = {
        "history": [
            {
                "labelsRemoved": [
                    {
                        "message": _msg("t1", ["INBOX"]),
                        "labelIds": ["Label_35"],
                    },
                ],
            },
        ],
    }
    assert _collect_project_thread_ids(history, PROJECT_LABELS) == {"t1"}


def test_labelsRemoved_for_non_project_label_drops_thread():
    # User cleared the UNREAD label by opening the email — irrelevant.
    history = {
        "history": [
            {
                "labelsRemoved": [
                    {
                        "message": _msg("t1", ["INBOX"]),
                        "labelIds": ["UNREAD"],
                    },
                ],
            },
        ],
    }
    assert _collect_project_thread_ids(history, PROJECT_LABELS) == set()


def test_mixed_threads_only_project_ones_pass():
    history = {
        "history": [
            {
                "messagesAdded": [
                    {"message": _msg("t_project", ["INBOX", "Label_35"])},
                    {"message": _msg("t_noise", ["INBOX", "CATEGORY_PROMOTIONS"])},
                ],
                "labelsRemoved": [
                    {
                        "message": _msg("t_other_noise", ["INBOX"]),
                        "labelIds": ["UNREAD"],
                    },
                ],
            },
        ],
    }
    assert _collect_project_thread_ids(history, PROJECT_LABELS) == {"t_project"}


def test_empty_project_label_set_drops_everything():
    # Fresh mailbox with no Notion projects yet — every push is noise.
    history = {
        "history": [
            {
                "messagesAdded": [
                    {"message": _msg("t1", ["INBOX", "Label_35"])},
                ],
            },
        ],
    }
    assert _collect_project_thread_ids(history, set()) == set()


def test_history_has_any_changes_true_for_messagesAdded():
    assert _history_has_any_changes(
        {"history": [{"messagesAdded": [{"message": _msg("t1", [])}]}]}
    )


def test_history_has_any_changes_true_for_labelsRemoved():
    assert _history_has_any_changes(
        {
            "history": [
                {"labelsRemoved": [{"message": _msg("t1", []), "labelIds": ["UNREAD"]}]}
            ]
        }
    )


def test_history_has_any_changes_false_for_empty_response():
    assert not _history_has_any_changes({"history": []})
    assert not _history_has_any_changes({})


def test_history_has_any_changes_false_when_only_unsupported_change_types():
    # If Gmail sends us a history entry with only types we don't track, treat
    # it as empty. (Today the only ones we don't watch are message deletions,
    # which don't need a Notion-side action.)
    assert not _history_has_any_changes(
        {"history": [{"messagesDeleted": [{"message": _msg("t1", [])}]}]}
    )
