"""Distinguish a routine review from an actual deliverable.

The owner requires ordinary scheduled work to remain in its task conversation.
This does not implement a daily/weekly delivery policy or suppress failures.
"""


def routine_review(event, reply):
    return (
        event["kind"] == "timer"
        and event.get("body", {}).get("output_kind") == "review"
        and not reply.get("error")
        and not reply.get("reviewed_result")
        and not reply.get("published_reports")
    )
