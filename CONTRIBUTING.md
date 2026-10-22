# Contributing

Use Python 3.11+ and install from the checkout with `python -m pip install -e .`. Run `python -m unittest discover -s tests -v`.

For a new review rule, include a triggering example, a near miss, its uncertainty, and an actionable recommendation. Separate measured results from static hypotheses. For a source transformation, demonstrate preserved outputs, dependency refusal, exception behavior, and cleanup after cancellation.

For routing, cache, or scheduler changes, include a regression case showing the behavior that would otherwise break. Do not introduce proprietary prompts, customer inputs, recordings, access tokens, or operational logs.

Describe the problem, resulting behavior, evidence, and remaining limits in a pull request. Keep automatic edits opt-in and narrowly scoped. Contributions are provided under the project's Apache-2.0 license.
