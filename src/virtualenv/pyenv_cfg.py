from __future__ import absolute_import, unicode_literals

import logging


class PyEnvCfg(object):
    def __init__(self, content, path):
        self.content = content
        self.path = path

    @classmethod
    def from_folder(cls, folder):
        return cls.from_file(folder / "pyvenv.cfg")

    @classmethod
    def from_file(cls, path):
        content = cls._read_values(path) if path.exists() else {}
        return PyEnvCfg(content, path)

    @staticmethod
    def _read_values(path):
        content = {}
        for line in path.read_text().splitlines():
            equals_at = line.index("=")
            key = line[:equals_at].strip()
            value = line[equals_at + 1 :].strip()
            content[key] = value
        return content

    def write(self):
        with open(str(self.path), "wt") as file_handler:
            logging.debug("write %s", self.path)
            for key, value in self.content.items():
                line = "{} = {}".format(key, value)
                logging.debug("\t%s", line)
                file_handler.write(line)
                file_handler.write("\n")

    def refresh(self):
        self.content = self._read_values(self.path)
        return self.content

    def __setitem__(self, key, value):
        self.content[key] = value

    def __getitem__(self, key):
        return self.content[key]

    def __contains__(self, item):
        return item in self.content

    def update(self, other):
        self.content.update(other)
        return self