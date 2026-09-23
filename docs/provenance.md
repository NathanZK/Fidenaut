# Extraction provenance

- **Project:** Fidenaut
- **Originating project:** [NathanZK/ChessEcho](https://github.com/NathanZK/ChessEcho)
- **Canonical/reference first consumer:** ChessEcho
- **Source revision:** `1ff0186050731acf00eb1669073afe7efd190af7`
- **Workflow origin:** `93eb11a15f6731f86b7d6c973765e56f2a705da6`
- **Extraction date:** 2026-09-23
- **History mechanism:** `git-filter-repo` 2.47.0, retaining the approved workflow-owned paths without author, date, or message rewriting
- **Extraction boundary commit:** `Extract governed autonomy workflow from ChessEcho`
- **License:** none selected; no `LICENSE` file is provided

Fidenaut is an independent extraction from ChessEcho. It did not exist as an independent project before this extraction and is not presented as predating it. Future projects may consume it at explicit revisions.

The extraction preserves the existing distinction between authoritative source/executable identity and human-readable provider-version labels. It also preserves the known limitations: local approval acknowledgments are self-attested rather than independently authorized, and role strings do not authenticate actor identity. No unsupported CAS, host-attestation, or provider-trust guarantee is claimed.

The retained workflow implementation, supervisor, role contracts, regression tests, configuration, and workflow documentation are the approved provider-owned boundary. ChessEcho application code, application CI, consumer repository conventions, PR-template tests, and other consumer-specific files remain excluded.
