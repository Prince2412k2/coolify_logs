"""Compatibility wrapper for running without installation."""

from logify_cli.main import main


if __name__ == "__main__":
    raise SystemExit(main())
