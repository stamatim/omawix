# Contributing

Omawix targets current Omarchy on Arch Linux and Python 3.11 or newer. Development checks also require Bash and a supported Node.js release.

## Workflow

1. Open an issue before starting a substantial behavior or interface change.
2. Keep changes focused and preserve the existing Omarchy-native interface.
3. Run `./check`. On an Omarchy system this includes plugin validation; elsewhere that step is skipped.
4. Manually exercise affected desktop or panel behavior when a change touches the UI or system integration.
5. Open a pull request that explains the behavior change, test results, and any user-visible screenshots.

Do not commit downloaded archives, runtime databases, aria2 state, local environment files, or generated artifacts. Security reports follow [SECURITY.md](SECURITY.md).
