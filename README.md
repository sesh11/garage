# Garage

Garage is a local incident monitoring and software dispatch service. The repository is being split from ambient-home in phases; phase A publishes the `garage-sdk` package, while the Garage service migration is planned for phase B.

## Install from Git

Install the SDK:

```sh
pip install "garage-sdk @ git+https://github.com/sesh11/garage.git@devin/1793294366-garage-split#subdirectory=packages/garage-sdk"
```

Install both distributions:

```sh
pip install \
  "garage-sdk @ git+https://github.com/sesh11/garage.git@devin/1793294366-garage-split#subdirectory=packages/garage-sdk" \
  "garage @ git+https://github.com/sesh11/garage.git@devin/1793294366-garage-split"
```

See [implemented versus future work](docs/implemented-vs-future.md), the [migration notes](docs/migration.md), and [history evidence](docs/history-evidence.md).
