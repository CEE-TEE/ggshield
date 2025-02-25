import codecs
import concurrent.futures
import logging
import re
from ast import literal_eval
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

import charset_normalizer
import click
from pygitguardian import GGClient
from pygitguardian.config import DOCUMENT_SIZE_THRESHOLD_BYTES, MULTI_DOCUMENT_LIMIT
from pygitguardian.models import Detail, ScanResult

from ggshield.core.cache import Cache
from ggshield.core.extra_headers import get_headers
from ggshield.core.filter import (
    is_filepath_excluded,
    remove_ignored_from_result,
    remove_results_from_ignore_detectors,
)
from ggshield.core.git_shell import GIT_PATH, shell
from ggshield.core.text_utils import STYLE, display_error, format_text, pluralize
from ggshield.core.types import IgnoredMatch
from ggshield.core.utils import REGEX_HEADER_INFO, Filemode, ScanContext

from ..iac.models import IaCScanResult


logger = logging.getLogger(__name__)

_RX_HEADER_LINE_SEPARATOR = re.compile("[\n\0]:", re.MULTILINE)


def _parse_patch_header_line(line: str) -> Tuple[str, Filemode]:
    """
    Parse a file line in the raw patch header, returns a tuple of filename, filemode

    See https://github.com/git/git/blob/master/Documentation/diff-format.txt for details
    on the format.
    """

    prefix, name, *rest = line.rstrip("\0").split("\0")

    if rest:
        # If the line has a new name, we want to use it
        name = rest[0]

    # for a non-merge commit, prefix is
    # :old_perm new_perm old_sha new_sha status_and_score
    #
    # for a 2 parent commit, prefix is
    # ::old_perm1 old_perm2 new_perm old_sha1 old_sha2 new_sha status_and_score
    #
    # We can ignore most of it, because we only care about the status.
    #
    # status_and_score is one or more status letters, followed by an optional numerical
    # score. We can ignore the score, but we need to check the status letters.
    status = prefix.rsplit(" ", 1)[-1].rstrip("0123456789")

    # There is one status letter per commit parent. In the case of a non-merge commit
    # the situation is simple: there is only one letter.
    # In the case of a merge commit we must look at all letters: if one parent is marked
    # as D(eleted) and the other as M(odified) then we use MODIFY as filemode because
    # the end result contains modifications. To ensure this, the order of the `if` below
    # matters.

    if "M" in status:  # modify
        return name, Filemode.MODIFY
    elif "C" in status:  # copy
        return name, Filemode.NEW
    elif "A" in status:  # add
        return name, Filemode.NEW
    elif "T" in status:  # type change
        return name, Filemode.NEW
    elif "R" in status:  # rename
        return name, Filemode.RENAME
    elif "D" in status:  # delete
        return name, Filemode.DELETE
    else:
        raise ValueError(f"Can't parse header line {line}: unknown status {status}")


def _parse_patch_header(header: str) -> Iterable[Tuple[str, Filemode]]:
    """
    Parse the header of a raw patch, generated with -z --raw
    """

    if header[0] == ":":
        # If the patch has been generated by `git diff` and not by `git show` then
        # there is no commit info and message, add a blank line to simulate commit info
        # otherwise the split below is going to skip the first file of the patch.
        header = "\n" + header

    # First item returned by split() contains commit info and message, skip it
    for line in _RX_HEADER_LINE_SEPARATOR.split(header)[1:]:
        yield _parse_patch_header_line(f":{line}")


class PatchParseError(Exception):
    """
    Raised by Commit.get_files() if it fails to parse its patch.
    """

    pass


class Result(NamedTuple):
    """
    Return model for a scan which zips the information
    between the Scan result and its input content.
    """

    content: str  # Text content scanned
    filemode: Filemode  # Filemode (useful for commits)
    filename: str  # Filename of content scanned
    scan: ScanResult  # Result of content scan


class Error(NamedTuple):
    files: List[Tuple[str, Filemode]]
    description: str  # Description of the error


@dataclass(frozen=True)
class Results:
    """
    Return model for a scan with the results and errors of the scan

    Not a NamedTuple like the others because it causes mypy 0.961 to crash on the
    `from_exception()` method (!)

    Similar crash: https://github.com/python/mypy/issues/12629
    """

    results: List[Result]
    errors: List[Error]

    @staticmethod
    def from_exception(exc: Exception) -> "Results":
        """Create a Results representing a failure"""
        error = Error(files=[], description=str(exc))
        return Results(results=[], errors=[error])


class ScanCollection(NamedTuple):
    id: str
    type: str
    results: Optional[Results] = None
    scans: Optional[List["ScanCollection"]] = None  # type: ignore[misc]
    iac_result: Optional[IaCScanResult] = None
    optional_header: Optional[str] = None  # To be printed in Text Output
    extra_info: Optional[Dict[str, str]] = None  # To be included in JSON Output

    @property
    def scans_with_results(self) -> List["ScanCollection"]:
        if self.scans:
            return [scan for scan in self.scans if scan.results]
        return []

    @property
    def has_iac_result(self) -> bool:
        return bool(self.iac_result and self.iac_result.entities_with_incidents)

    @property
    def has_results(self) -> bool:
        return bool(self.results and self.results.results)

    def get_all_results(self) -> Iterable[Result]:
        """Returns an iterable on all results and sub-scan results"""
        if self.results:
            yield from self.results.results
        if self.scans:
            for scan in self.scans:
                yield from scan.results.results


class File:
    """Class representing a simple file."""

    def __init__(self, document: Optional[str], filename: str):
        self._document = document
        self.filename = filename
        self.filemode = Filemode.FILE

    def relative_to(self, root_path: Path) -> "File":
        return File(self._document, str(Path(self.filename).relative_to(root_path)))

    @property
    def document(self) -> str:
        self.prepare()
        assert self._document is not None
        return self._document

    def prepare(self) -> None:
        """Ensures self._document has been decoded"""
        if self._document is not None:
            return
        with open(self.filename, "rb") as f:
            self._document = File._decode_bytes(f.read(), self.filename)

    @staticmethod
    def from_bytes(raw_document: bytes, filename: str) -> "File":
        """Creates a File instance for a raw document. Document is decoded immediately."""
        document = File._decode_bytes(raw_document, filename)
        return File(document, filename)

    @staticmethod
    def from_path(filename: str) -> "File":
        """Creates a File instance for a file. Content is *not* read immediately."""
        return File(None, filename)

    @staticmethod
    def _decode_bytes(raw_document: bytes, filename: str) -> str:
        """Low level function to decode bytes, tries hard to find the correct encoding.
        For now it returns an empty string if the document could not be decoded"""
        result = charset_normalizer.from_bytes(raw_document).best()
        if result is None:
            # This means we were not able to detect the encoding. Report it using logging for now
            # TODO: we should report this in the output
            logger.warning("Skipping %s, can't detect encoding", filename)
            return ""

        # Special case for utf_8 + BOM: `bytes.decode()` does not skip the BOM, so do it
        # ourselves
        if result.encoding == "utf_8" and raw_document.startswith(codecs.BOM_UTF8):
            raw_document = raw_document[len(codecs.BOM_UTF8) :]
        return raw_document.decode(result.encoding, errors="replace")

    def __repr__(self) -> str:
        return f"<File filename={self.filename} filemode={self.filemode}>"

    def has_extensions(self, extensions: Set[str]) -> bool:
        """Returns True iff the file has one of the given extensions."""
        file_extensions = Path(self.filename).suffixes
        return any(ext in extensions for ext in file_extensions)


class CommitFile(File):
    """Class representing a commit file."""

    def __init__(self, document: str, filename: str, filemode: Filemode):
        super().__init__(document, filename)
        self.filemode = filemode


class Files:
    """
    Files is a list of files. Useful for directory scanning.
    """

    def __init__(self, files: List[File]):
        self._files = files

    @property
    def files(self) -> List[File]:
        """The list of files owned by this instance. The same filename can appear twice,
        in case of a merge commit."""
        return self._files

    @property
    def filenames(self) -> List[str]:
        """Convenience property to list filenames in the same order as files"""
        return [x.filename for x in self.files]

    def __repr__(self) -> str:
        return f"<Files files={self.files}>"

    def apply_filter(self, filter_func: Callable[[File], bool]) -> "Files":
        return Files([file for file in self.files if filter_func(file)])

    def relative_to(self, root_path: Path) -> "Files":
        return Files([file.relative_to(root_path) for file in self.files])

    def scan(
        self,
        client: GGClient,
        cache: Cache,
        matches_ignore: Iterable[IgnoredMatch],
        scan_context: ScanContext,
        ignored_detectors: Optional[Set[str]] = None,
        progress_callback: Callable[..., None] = lambda advance: None,
        scan_threads: int = 4,
    ) -> Results:
        logger.debug("self=%s command_id=%s", self, scan_context.command_id)
        scanner = Scanner(
            client, cache, matches_ignore, scan_context, ignored_detectors
        )
        return scanner.scan(self.files, progress_callback, scan_threads)


class Scanner:
    def __init__(
        self,
        client: GGClient,
        cache: Cache,
        matches_ignore: Iterable[IgnoredMatch],
        scan_context: ScanContext,
        ignored_detectors: Optional[Set[str]] = None,
    ):
        self.client = client
        self.cache = cache
        self.matches_ignore = matches_ignore
        self.ignored_detectors = ignored_detectors
        self.headers = get_headers(scan_context)

    def _scan_chunk(
        self, executor: concurrent.futures.ThreadPoolExecutor, chunk: List[File]
    ) -> Future:
        """
        Sends a chunk of files to scan to the API
        """
        # `documents` is a version of `chunk` suitable for `GGClient.multi_content_scan()`
        documents = [{"document": x.document, "filename": x.filename} for x in chunk]
        return executor.submit(
            self.client.multi_content_scan,
            documents,
            self.headers,
        )

    def _start_scans(
        self,
        executor: concurrent.futures.ThreadPoolExecutor,
        files: Iterable[File],
        progress_callback: Callable[..., None],
    ) -> Dict[Future, List[File]]:
        """
        Start all scans, return a tuple containing:
        - a mapping of future to the list of files it is scanning
        - a list of files which we did not send to scan because we could not decode them

        on_file
        """
        chunks_for_futures = {}
        skipped_chunk = []

        chunk: List[File] = []
        for file in files:
            if file.document:
                chunk.append(file)
                if len(chunk) == MULTI_DOCUMENT_LIMIT:
                    future = self._scan_chunk(executor, chunk)
                    progress_callback(advance=len(chunk))
                    chunks_for_futures[future] = chunk
                    chunk = []
            else:
                skipped_chunk.append(file)
        if chunk:
            future = self._scan_chunk(executor, chunk)
            progress_callback(advance=len(chunk))
            chunks_for_futures[future] = chunk
        progress_callback(advance=len(skipped_chunk))
        return chunks_for_futures

    def _collect_results(
        self,
        chunks_for_futures: Dict[Future, List[File]],
    ) -> Results:
        """
        Receive scans as they complete, report progress and collect them and return
        a Results.
        """
        self.cache.purge()

        results = []
        errors = []
        for future in concurrent.futures.as_completed(chunks_for_futures):
            chunk = chunks_for_futures[future]

            exception = future.exception()
            if exception is None:
                scan = future.result()
            else:
                scan = Detail(detail=str(exception))
                errors.append(
                    Error(
                        files=[(file.filename, file.filemode) for file in chunk],
                        description=scan.detail,
                    )
                )

            if not scan.success:
                handle_scan_chunk_error(scan, chunk)
                continue

            for file, scanned in zip(chunk, scan.scan_results):
                remove_ignored_from_result(scanned, self.matches_ignore)
                remove_results_from_ignore_detectors(scanned, self.ignored_detectors)
                if scanned.has_policy_breaks:
                    for policy_break in scanned.policy_breaks:
                        self.cache.add_found_policy_break(policy_break, file.filename)
                    results.append(
                        Result(
                            content=file.document,
                            scan=scanned,
                            filemode=file.filemode,
                            filename=file.filename,
                        )
                    )

        self.cache.save()
        return Results(results=results, errors=errors)

    def scan(
        self,
        files: Iterable[File],
        progress_callback: Callable[..., None],
        scan_threads: int = 4,
    ) -> Results:

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=scan_threads, thread_name_prefix="content_scan"
        ) as executor:
            chunks_for_futures = self._start_scans(
                executor,
                files,
                progress_callback,
            )

            return self._collect_results(chunks_for_futures)


class CommitInformation(NamedTuple):
    author: str
    email: str
    date: str


def _parse_patch(
    patch: str, exclusion_regexes: Set[re.Pattern]
) -> Iterable[CommitFile]:
    """
    Parse the patch generated with `git show` (or `git diff`)

    If the patch represents a merge commit, then `patch` actually contains multiple
    commits, one per parent, because we call `git show` with the `-m` option to force it
    to generate one single-parent commit per parent. This makes later code simpler and
    ensures we see *all* the changes.
    """
    for commit in patch.split("\0commit "):
        tokens = commit.split("\0diff ", 1)
        if len(tokens) == 1:
            # No diff, carry on to next commit
            continue
        header, rest = tokens

        names_and_modes = _parse_patch_header(header)

        diffs = re.split(r"^diff ", rest, flags=re.MULTILINE)
        for (filename, filemode), diff in zip(names_and_modes, diffs):
            if is_filepath_excluded(filename, exclusion_regexes):
                continue

            # extract document from diff: we must skip diff extended headers
            # (lines like "old mode 100644", "--- a/foo", "+++ b/foo"...)
            try:
                end_of_headers = diff.index("\n@@")
            except ValueError:
                # No content
                continue
            # +1 because we searched for the '\n'
            document = diff[end_of_headers + 1 :]

            file_size = len(document.encode("utf-8"))
            if file_size > DOCUMENT_SIZE_THRESHOLD_BYTES * 0.90:
                continue

            if document:
                yield CommitFile(document, filename, filemode)


class Commit(Files):
    """
    Commit represents a commit which is a list of commit files.
    """

    def __init__(
        self, sha: Optional[str] = None, exclusion_regexes: Set[re.Pattern] = set()
    ):
        self.sha = sha
        self._patch: Optional[str] = None
        self._files = []
        self.exclusion_regexes = exclusion_regexes
        self._info: Optional[CommitInformation] = None

    @property
    def info(self) -> CommitInformation:
        if self._info is None:
            m = REGEX_HEADER_INFO.search(self.patch)

            if m is None:
                self._info = CommitInformation("unknown", "", "")
            else:
                self._info = CommitInformation(**m.groupdict())

        return self._info

    @property
    def optional_header(self) -> str:
        """Return the formatted patch."""
        return (
            format_text(f"\ncommit {self.sha}\n", STYLE["commit_info"])
            + f"Author: {self.info.author} <{self.info.email}>\n"
            + f"Date: {self.info.date}\n"
        )

    @property
    def patch(self) -> str:
        """Get the change patch for the commit."""
        if self._patch is None:
            common_args = [
                "--raw",  # shows a header with the files touched by the commit
                "-z",  # separate file names in the raw header with \0
                "--patch",  # force output of the diff (--raw disables it)
                "-m",  # split multi-parent (aka merge) commits into several one-parent commits
            ]
            if self.sha:
                self._patch = shell([GIT_PATH, "show", self.sha] + common_args)
            else:
                self._patch = shell([GIT_PATH, "diff", "--cached"] + common_args)

        return self._patch

    @property
    def files(self) -> List[File]:
        if not self._files:
            self._files = list(self.get_files())

        return self._files

    def get_files(self) -> Iterable[CommitFile]:
        """
        Parse the patch into files and extract the changes for each one of them.

        See tests/data/patches for examples
        """
        try:
            yield from _parse_patch(self.patch, self.exclusion_regexes)
        except Exception as exc:
            raise PatchParseError(f"Could not parse patch (sha: {self.sha}): {exc}")

    def __repr__(self) -> str:
        return f"<Commit sha={self.sha} files={self.files}>"


def handle_scan_chunk_error(detail: Detail, chunk: List[File]) -> None:
    logger.error("status_code=%d detail=%s", detail.status_code, detail.detail)
    if detail.status_code == 401:
        raise click.UsageError(detail.detail)
    if detail.status_code is None:
        raise click.ClickException(
            f"Error scanning, network error occurred: {detail.detail}"
        )

    details = None

    display_error("\nError scanning. Results may be incomplete.")
    try:
        # try to load as list of dicts to get per file details
        details = literal_eval(detail.detail)
    except Exception:
        pass

    if isinstance(details, list) and details:
        # if the details had per file details
        display_error(
            f"Add the following {pluralize('file', len(details))}"
            " to your paths-ignore:"
        )
        for i, inner_detail in enumerate(details):
            if inner_detail:
                click.echo(
                    f"- {format_text(chunk[i].filename, STYLE['filename'])}:"
                    f" {str(inner_detail)}",
                    err=True,
                )
        return
    else:
        # if the details had a request error
        filenames = ", ".join([file.filename for file in chunk])
        display_error(
            "The following chunk is affected:\n"
            f"{format_text(filenames, STYLE['filename'])}"
        )

        display_error(str(detail))
