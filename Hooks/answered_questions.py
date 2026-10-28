"""Prints the operator's answers this session read back from its own parking-lot page, for Invoke-SessionStart.ps1 to
put back in context after a compaction.

    python Harness/Hooks/answered_questions.py <transcript path>

A compaction summary paraphrases or drops an answer, and a session that lost one asks the question again. The
answers come from invoke_artifact_gate.page_questions, the same reader the hooks trust as the operator's words, so a
question this session ever wrote an answer to, or read from a page it did not publish, is left out. Prints nothing
when there is no such answer.
"""

import os
import sys

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import invoke_artifact_gate  # noqa: E402

HEADING = ("The operator's answers on this session's parking-lot page, read back before the compaction. They bind the "
           "work after them; never ask any of these again:")


def render(questions):
    lines = [HEADING]
    for question in questions:
        lines.append("- {0}: {1}".format(question["id"], question["question"]))
        lines.append("  Answered {0}: {1}".format(question["answeredAt"], question["answer"]))
    return "\n".join(lines)


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: answered_questions.py <transcript path>\n")
        return 2
    questions = invoke_artifact_gate.page_questions(argv[1])
    if questions:
        sys.stdout.buffer.write(render(questions).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
