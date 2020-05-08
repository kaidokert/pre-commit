import contextlib
import logging
import os.path
import sqlite3
import tempfile
from typing import Callable
from typing import Generator
from typing import List
from typing import Optional
from typing import Sequence
from typing import Tuple

import pre_commit.constants as C
from pre_commit import file_lock
from pre_commit import git
from pre_commit.util import CalledProcessError
from pre_commit.util import clean_path_on_failure
from pre_commit.util import cmd_output_b
from pre_commit.util import resource_text
from pre_commit.util import rmtree


logger = logging.getLogger('pre_commit')


def _get_default_directory() -> str:
    """Returns the default directory for the Store.  This is intentionally
    underscored to indicate that `Store.get_default_directory` is the intended
    way to get this information.  This is also done so
    `Store.get_default_directory` can be mocked in tests and
    `_get_default_directory` can be tested.
    """
    ret = os.environ.get('PRE_COMMIT_HOME') or os.path.join(
        os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
        'pre-commit',
    )
    return os.path.realpath(ret)


class Store:
    get_default_directory = staticmethod(_get_default_directory)

    def __init__(self, directory: Optional[str] = None) -> None:
        self.directory = directory or Store.get_default_directory()
        self.db_path = os.path.join(self.directory, 'db.db')

        if not os.path.exists(self.directory):
            os.makedirs(self.directory, exist_ok=True)
            with open(os.path.join(self.directory, 'README'), 'w') as f:
                f.write(
                    'This directory is maintained by the pre-commit project.\n'
                    'Learn more: https://github.com/pre-commit/pre-commit\n',
                )

        if os.path.exists(self.db_path):
            return
        with self.exclusive_lock():
            # Another process may have already completed this work
            if os.path.exists(self.db_path):  # pragma: no cover (race)
                return
            # To avoid a race where someone ^Cs between db creation and
            # execution of the CREATE TABLE statement
            fd, tmpfile = tempfile.mkstemp(dir=self.directory)
            # We'll be managing this file ourselves
            os.close(fd)
            with self.connect(db_path=tmpfile) as db:
                db.executescript(
                    'CREATE TABLE repos ('
                    '    repo TEXT NOT NULL,'
                    '    ref TEXT NOT NULL,'
                    '    path TEXT NOT NULL,'
                    '    PRIMARY KEY (repo, ref)'
                    ');',
                )
                self._create_config_table(db)

            # Atomic file move
            os.rename(tmpfile, self.db_path)

    @contextlib.contextmanager
    def exclusive_lock(self) -> Generator[None, None, None]:
        def blocked_cb() -> None:  # pragma: no cover (tests are in-process)
            logger.info('Locking pre-commit directory')

        with file_lock.lock(os.path.join(self.directory, '.lock'), blocked_cb):
            yield

    @contextlib.contextmanager
    def connect(
            self,
            db_path: Optional[str] = None,
    ) -> Generator[sqlite3.Connection, None, None]:
        db_path = db_path or self.db_path
        # sqlite doesn't close its fd with its contextmanager >.<
        # contextlib.closing fixes this.
        # See: https://stackoverflow.com/a/28032829/812183
        with contextlib.closing(sqlite3.connect(db_path)) as db:
            # this creates a transaction
            with db:
                yield db

    @classmethod
    def db_repo_name(cls, repo: str, deps: Sequence[str]) -> str:
        if deps:
            return f'{repo}:{",".join(sorted(deps))}'
        else:
            return repo

    def _new_repo(
            self,
            repo: str,
            ref: str,
            deps: Sequence[str],
            make_strategy: Callable[[str], None],
    ) -> str:
        repo = self.db_repo_name(repo, deps)

        def _get_result() -> Optional[str]:
            # Check if we already exist
            with self.connect() as db:
                result = db.execute(
                    'SELECT path FROM repos WHERE repo = ? AND ref = ?',
                    (repo, ref),
                ).fetchone()
                return result[0] if result else None

        result = _get_result()
        if result:
            return result
        with self.exclusive_lock():
            # Another process may have already completed this work
            result = _get_result()
            if result:  # pragma: no cover (race)
                return result

            logger.info(f'Initializing environment for {repo}.')

            directory = tempfile.mkdtemp(prefix='repo', dir=self.directory)
            with clean_path_on_failure(directory):
                make_strategy(directory)

            # Update our db with the created repo
            with self.connect() as db:
                db.execute(
                    'INSERT INTO repos (repo, ref, path) VALUES (?, ?, ?)',
                    [repo, ref, directory],
                )
        return directory

    def _complete_clone(self, ref: str, git_cmd: Callable[..., None]) -> None:
        """Perform a complete clone of a repository and its submodules """

        git_cmd('fetch', 'origin', '--tags')
        git_cmd('checkout', ref)
        git_cmd('submodule', 'update', '--init', '--recursive')

    def _shallow_clone(self, ref: str, git_cmd: Callable[..., None]) -> None:
        """Perform a shallow clone of a repository and its submodules """

        git_config = 'protocol.version=2'
        git_cmd('-c', git_config, 'fetch', 'origin', ref, '--depth=1')
        git_cmd('checkout', 'FETCH_HEAD')
        git_cmd(
            '-c', git_config, 'submodule', 'update', '--init', '--recursive',
            '--depth=1',
        )

    def clone(self, repo: str, ref: str, deps: Sequence[str] = ()) -> str:
        """Clone the given url and checkout the specific ref."""

        def clone_strategy(directory: str) -> None:
            git.init_repo(directory, repo)
            env = git.no_git_env()

            def _git_cmd(*args: str) -> None:
                cmd_output_b('git', *args, cwd=directory, env=env)

            try:
                self._shallow_clone(ref, _git_cmd)
            except CalledProcessError:
                self._complete_clone(ref, _git_cmd)

        return self._new_repo(repo, ref, deps, clone_strategy)

    LOCAL_RESOURCES = (
        'Cargo.toml', 'main.go', 'go.mod', 'main.rs', '.npmignore',
        'package.json', 'pre_commit_dummy_package.gemspec', 'setup.py',
        'environment.yml', 'Makefile.PL',
    )

    def make_local(self, deps: Sequence[str]) -> str:
        def make_local_strategy(directory: str) -> None:
            for resource in self.LOCAL_RESOURCES:
                contents = resource_text(f'empty_template_{resource}')
                with open(os.path.join(directory, resource), 'w') as f:
                    f.write(contents)

            env = git.no_git_env()

            # initialize the git repository so it looks more like cloned repos
            def _git_cmd(*args: str) -> None:
                cmd_output_b('git', *args, cwd=directory, env=env)

            git.init_repo(directory, '<<unknown>>')
            _git_cmd('add', '.')
            git.commit(repo=directory)

        return self._new_repo(
            'local', C.LOCAL_REPO_VERSION, deps, make_local_strategy,
        )

    def _create_config_table(self, db: sqlite3.Connection) -> None:
        db.executescript(
            'CREATE TABLE IF NOT EXISTS configs ('
            '   path TEXT NOT NULL,'
            '   PRIMARY KEY (path)'
            ');',
        )

    def mark_config_used(self, path: str) -> None:
        path = os.path.realpath(path)
        # don't insert config files that do not exist
        if not os.path.exists(path):
            return
        with self.connect() as db:
            # TODO: eventually remove this and only create in _create
            self._create_config_table(db)
            db.execute('INSERT OR IGNORE INTO configs VALUES (?)', (path,))

    def select_all_configs(self) -> List[str]:
        with self.connect() as db:
            self._create_config_table(db)
            rows = db.execute('SELECT path FROM configs').fetchall()
            return [path for path, in rows]

    def delete_configs(self, configs: List[str]) -> None:
        with self.connect() as db:
            rows = [(path,) for path in configs]
            db.executemany('DELETE FROM configs WHERE path = ?', rows)

    def select_all_repos(self) -> List[Tuple[str, str, str]]:
        with self.connect() as db:
            return db.execute('SELECT repo, ref, path from repos').fetchall()

    def delete_repo(self, db_repo_name: str, ref: str, path: str) -> None:
        with self.connect() as db:
            db.execute(
                'DELETE FROM repos WHERE repo = ? and ref = ?',
                (db_repo_name, ref),
            )
        rmtree(path)
