# Setup prompt for your coding agent

The existing general-purpose multi-agent harness is in this folder.
**Do not rewrite it from scratch.** Install it and extend only where tests or real
integration checks show a gap.

1. Inspect README.md and the existing Python package.
2. Run `py -m unittest discover -s tests -v` and fix any failures.
3. Run `py -m pip install -e .`; ensure `smart-coder` works globally on Windows.
4. Run mock mode from a directory that is NOT a Git repository. Verify `status`,
   history, and worker artifacts.
5. Verify the currently installed OpenCode CLI supports `run --format json`,
   `--model`, `--agent`, and global agent definitions.
6. Verify configured model IDs and the simultaneous OpenRouter/OpenAI auth setup.
7. Keep the director tool-less and restricted to the three JSON actions.
8. Preserve the generic nature: do not add Git/repository assumptions or mandatory
   project roots. Worker tools determine what resources are available for any task.
9. Strengthen tests for failure recovery, bounded worker/model costs, and
   concurrent independent read-only assignments. Do not rebuild working modules.
10. No credentials are available yet. Do not request secrets, authenticate, or
    make paid API calls. Leave clearly stated manual integration steps for later.

Finish with passing offline tests, a working global command, and a concise report
of what remains unverified until credentials are supplied.
