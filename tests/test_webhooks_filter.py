"""Project-label filter on Gmail history responses.

Pins the two-bucket split `_collect_project_thread_ids` produces:

- `definite` — threads the history payload alone proves belong to a project
  (a message carrying a project label, or a project label just added/removed).
- `candidates` — messagesAdded threads whose new message lacked a project
  label. A reply or our own SENT mail on an already-labeled thread only gets
  system labels (INBOX/SENT/UNREAD), so we can't tell from the payload alone;
  the caller resolves these against the local EmailRow cache. Genuine noise
  (UNREAD toggles, CATEGORY_UPDATES on unknown threads) also lands here and is
  dropped by that lookup — which is what keeps docker logs silent.
"""

from __future__ import annotations

from gb_automations.routes.webhooks import (
    _collect_project_thread_ids,
    _history_has_any_changes,
)

PROJECT_LABELS = {"Label_35", "Label_38"}  # two project leaves


def _msg(thread_id: str, label_ids: list[str], msg_id: str = "m1") -> dict:
    return {"id": msg_id, "threadId": thread_id, "labelIds": label_ids}


def test_messagesAdded_with_project_label_is_definite():
    history = {
        "history": [
            {
                "messagesAdded": [
                    {"message": _msg("t1", ["INBOX", "Label_35"])},
                ],
            },
        ],
    }
    definite, candidates = _collect_project_thread_ids(history, PROJECT_LABELS)
    assert definite == {"t1"}
    assert candidates == set()


def test_messagesAdded_without_project_label_is_candidate():
    # The reply / new-message case: the new message carries no project label,
    # so we can't decide from the payload alone — it becomes a candidate for
    # the caller's EmailRow lookup, NOT dropped at parse time.
    history = {
        "history": [
            {
                "messagesAdded": [
                    {"message": _msg("t1", ["INBOX", "CATEGORY_UPDATES"])},
                ],
            },
        ],
    }
    definite, candidates = _collect_project_thread_ids(history, PROJECT_LABELS)
    assert definite == set()
    assert candidates == {"t1"}


def test_messagesAdded_sent_reply_is_candidate():
    # Our own reply: SENT only, no project label. Must surface as a candidate
    # so the caller can recover it via the known-thread cache.
    history = {
        "history": [
            {
                "messagesAdded": [
                    {"message": _msg("t1", ["SENT"])},
                ],
            },
        ],
    }
    definite, candidates = _collect_project_thread_ids(history, PROJECT_LABELS)
    assert definite == set()
    assert candidates == {"t1"}


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
    definite, candidates = _collect_project_thread_ids(history, PROJECT_LABELS)
    assert definite == {"t1"}
    assert candidates == set()


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
    definite, candidates = _collect_project_thread_ids(history, PROJECT_LABELS)
    assert definite == set()
    assert candidates == set()


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
    definite, candidates = _collect_project_thread_ids(history, PROJECT_LABELS)
    assert definite == {"t1"}
    assert candidates == set()


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
    definite, candidates = _collect_project_thread_ids(history, PROJECT_LABELS)
    assert definite == set()
    assert candidates == set()


def test_mixed_threads_split_into_definite_and_candidate():
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
    definite, candidates = _collect_project_thread_ids(history, PROJECT_LABELS)
    # t_project is proven by its label; t_noise needs a cache lookup;
    # t_other_noise (label-only change, no project label) is dropped outright.
    assert definite == {"t_project"}
    assert candidates == {"t_noise"}


def test_thread_with_labeled_and_bare_message_is_definite_only():
    # Same thread: one message carries the project label, another (a reply)
    # doesn't. The thread is already definite, so it must not also linger as a
    # candidate. The caller dedups with `candidates -= definite`, but pin that
    # both messages here resolve to the same authoritative bucket.
    history = {
        "history": [
            {
                "messagesAdded": [
                    {"message": _msg("t1", ["INBOX", "Label_35"], "m1")},
                    {"message": _msg("t1", ["INBOX"], "m2")},
                ],
            },
        ],
    }
    definite, candidates = _collect_project_thread_ids(history, PROJECT_LABELS)
    assert definite == {"t1"}
    assert candidates - definite == set()


def test_empty_project_label_set_yields_no_definite():
    # Fresh mailbox with no Notion projects yet — nothing is provably a
    # project thread. (Candidates may be populated, but the caller's EmailRow
    # cache is empty too, so the net result is no sync.)
    history = {
        "history": [
            {
                "messagesAdded": [
                    {"message": _msg("t1", ["INBOX", "Label_35"])},
                ],
            },
        ],
    }
    definite, _ = _collect_project_thread_ids(history, set())
    assert definite == set()


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
