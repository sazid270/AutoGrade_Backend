# Deptry

Tool to find unused dependencies, missing dependencies, and transitive dependencies in Python projects.

## Usage

```bash
deptry .
```

The configuration in `pyproject.toml` excludes generated and non-source directories and documents runtime dependencies that are configured indirectly.

## Error Codes

| Code   | Issue                 | Fix                                               |
| ------ | --------------------- | ------------------------------------------------- |
| DEP001 | Missing dependency    | Add it to `requirements.txt` and `pyproject.toml` |
| DEP002 | Unused dependency     | Remove it from both dependency lists              |
| DEP003 | Transitive dependency | Remove it if it is not directly needed            |
| DEP004 | Misplaced dependency  | Move it to the appropriate requirements file      |

Documentation: https://deptry.com/
