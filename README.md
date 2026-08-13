# `official/postgres`

Standalone home for Toka's official PostgreSQL package.

## Migration status

This repository is a migration scaffold and is not yet the canonical package
source. Until standalone qualification, release, and registry consumer replay
are complete, the authoritative source remains
[`tokalang/toka/official/postgres`](https://github.com/tokalang/toka/tree/main/official/postgres).

Cutover will be one-way. The compiler repository copy will be removed after a
successful standalone release; this repository will not become a long-lived
mirror or submodule. Deterministic protocol tests and optional real-service
qualification will be owned here after migration.

## License

Apache License 2.0. See [LICENSE](LICENSE).
Official PostgreSQL package for Toka
