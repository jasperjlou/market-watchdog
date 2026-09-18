# Security policy

Market Watchdog is designed as a read-only analysis project. Do not add broker credentials, messaging tokens, OAuth files, account identifiers, portfolio snapshots, production hostnames, or runtime logs to this repository.

The shipped configuration keeps broker writes and external sends disabled. A change that introduces order, cancel, modify, unlock, or transmit behavior must not be merged as part of this project.

If you discover a vulnerability, use GitHub's private vulnerability reporting feature instead of opening a public issue with exploit details or credentials.
