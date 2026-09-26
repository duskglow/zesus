# AI use policy

## How this project was made

Zesus was written with [Claude Code](https://claude.com/claude-code), using Anthropic's
Claude Opus 5.5 model, under the direction of a human maintainer. The maintainer:

* set the goals;
* made the design decisions;
* tested the tool against a real damaged pool;
* reviewed the results.

Commits co-written with AI carry a `Co-Authored-By` trailer that says so.

## AI-assisted contributions are welcome

You may use AI tools to write code, tests or documentation for Zesus. We care about the
quality of a contribution, not about which tools produced it.

## Your contribution is your responsibility

* **You are the author.** Whatever you submit, you are accountable for it: its correctness,
  its licensing, and its consequences. "The AI wrote it" is not an explanation for a bug, a
  hallucinated API, or a broken test. Low-quality contributions will be treated as
  low-quality contributions, whatever tool produced them.
* **Test it and review it yourself before you submit it.** Run the test suite. Exercise the
  change for real, against an image where that makes sense. Read every line. If you cannot
  explain what a change does and why it is correct, it is not ready.
* **Check the facts.** Anything that claims something about ZFS on-disk structures,
  filesystem formats or tool behaviour must be verified against the OpenZFS source,
  published documentation, or a real image. Do not trust a model's description alone.
  Recovery tools that act on invented facts destroy people's data, or give them false
  confidence.

## Commit messages and pull requests

* **Descriptive and to the point.** Say what changed and why. Skip the boilerplate,
  filler and marketing tone.
* **Free of hallucinations.** Describe only what the change actually does, how you actually
  tested it, and what you actually observed. Do not claim tests pass that you did not run,
  cite issues that do not exist, or describe behaviour you did not verify.
* **Disclose substantial AI help.** Add a trailer such as
  `Co-Authored-By: <model or tool>` or `Assisted-by: <tool>` to the commit message. It is not
  a penalty. It helps reviewers know where to look harder.

## This project's own rules still apply

AI assistance changes nothing in [CONTRIBUTING.md](CONTRIBUTING.md). In particular:

* nothing may write to the evidence;
* nothing may silently guess;
* parsers must survive damaged input;
* **never paste real case data (disk images, maps, file names, file contents) into an AI
  service** that you do not control. It is someone's private data.

Maintainers may ask how a change was produced and tested. They may decline contributions
that show signs of unreviewed generation, such as code that does not match its description,
tests that do not test anything, or confidently wrong explanations.
