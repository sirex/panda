# panda-repo starter template

Copy this directory into a fresh git repo and push it to make a Panda
questions/results repository.

```sh
cp -r repo-template my-panda-repo
cd my-panda-repo
git init -b main
git add .
git commit -m "seed"
git remote add origin git@github.com:you/panda-repo.git
git push -u origin main
```

Then point the game at it:

```sh
/path/to/panda.py init-repo git@github.com:you/panda-repo.git
```

## Layout

```
.
├── README.md
├── tests/
│   ├── math-easy.toml
│   └── math-easy.toml.sha256
└── results/
    └── .gitkeep
```

- Add one `.toml` per test in `tests/`.
- Run `./panda.py verify-tests` after editing tests to refresh the
  `.sha256` sidecars, then commit both the `.toml` and its `.sha256`.
- `results/` fills up with per-player session logs as kids play.