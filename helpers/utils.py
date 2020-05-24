# utils.py
# Copyright (C) 2020 Presidenza del Consiglio dei Ministri.
# Please refer to the AUTHORS file for more information.
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""General purpose utilities"""

import hashlib
import markdown
import os

from contextlib import contextmanager
from mdx_bleach.extension import BleachExtension
from typing import Dict, Iterable, Optional, Set

# Configure Markdown sanitizer
md = markdown.Markdown(extensions=[BleachExtension()])


@contextmanager
def cd(new_dir):
    """Temporarily change the working directory.

    :param new_dir: the directory to enter.
    """
    prev_dir = os.getcwd()
    os.chdir(os.path.expanduser(new_dir))
    try:
        yield
    finally:
        os.chdir(prev_dir)


def compute_files_hash(directory: str, filenames: Iterable[str]) -> Dict[str, Optional[str]]:
    """Compute the SHA256 hash of the specified filenames within the given directory.

    :param directory: the root directory containing the specified files.
    :param filenames: an iterable of file names to be found inside the specified directory.
    :return: a dictionary mapping each file to its SHA256 hash.
    """
    with cd(directory):
        file_hashes = {filename: _compute_file_hash(filename) for filename in filenames}

    return file_hashes


def get_files_by_hash_map(file_hashes: Dict[str, Optional[str]]) -> Set[str]:
    """Extract the list of files with non-null hashes from a hash map.

    :param file_hashes: a map of files with their SHA256 hash.
    :return: the list of files with non-null hashes of the specified hash map.
    """
    files = set()

    for file, sha in file_hashes.items():
        if sha is not None:
            files.add(file)

    return files


def sanitize_markdown(message: str) -> str:
    """Sanitize Markdown messages before they're printed on console and sent to GitHub.

    :param message: the message to be sanitized.
    :return: the sanitized message.
    """
    return md.convert(message)


def _compute_file_hash(filename: str) -> Optional[str]:
    """Compute the SHA256 hash of the specified file.

    :param filename: the name of the file whose hash must be computed.
    :return: the hash of the specified file, or None if the file does not exist.
    """
    sha256_hash = hashlib.sha256()

    try:
        with open(filename, "rb") as f:
            # Read and update hash string value in blocks of 4K.
            # This will allow us to parse large files as well.
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
    except FileNotFoundError:
        return None
