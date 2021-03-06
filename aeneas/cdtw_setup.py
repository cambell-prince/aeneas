#!/usr/bin/env python
# coding=utf-8

"""
Compile the Python C Extension for computing the DTW.

.. versionadded:: 1.1.0
"""

import os
import sys

from distutils.core import setup, Extension
from numpy.distutils import misc_util

__author__ = "Alberto Pettarin"
__copyright__ = """
    Copyright 2012-2013, Alberto Pettarin (www.albertopettarin.it)
    Copyright 2013-2015, ReadBeyond Srl   (www.readbeyond.it)
    Copyright 2015,      Alberto Pettarin (www.albertopettarin.it)
    """
__license__ = "GNU AGPL v3"
__version__ = "1.2.0"
__email__ = "aeneas@readbeyond.it"
__status__ = "Production"

for compiled in ["cdtw.so", "cdtw.dylib", "cdtw.dll"]:
    if os.path.exists(compiled):
        try:
            os.remove(compiled)
            print "[INFO] Removed file %s\n" % compiled
        except:
            pass

CMODULE = Extension("cdtw", sources=["cdtw.c"])

setup(
    name="cdtw",
    version="1.1.1",
    description="""
    Python C Extension for computing the DTW as fast as your bare metal allows.
    """,
    ext_modules=[CMODULE],
    include_dirs=misc_util.get_numpy_include_dirs()
)

print "\n[INFO] Module cdtw successfully compiled\n"
sys.exit(0)


