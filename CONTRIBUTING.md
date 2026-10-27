# Contributing

## Prerequisites

- Python 3.12

## Setup

Run `pip install -r requirements.txt pyinstaller==6.10.0` inside a Python 3.12 virtual environment.

## Pull requests

These checks must pass before a pull request is opened:

```sh
python -m unittest discover -s src -p "test_*.py"
pyinstaller pyinstaller.spec --clean --noconfirm && dist/vst-host --help
```

## Commits

Commits follow [Conventional Commits](https://www.conventionalcommits.org/). A commit body states the reasoning for the change.
