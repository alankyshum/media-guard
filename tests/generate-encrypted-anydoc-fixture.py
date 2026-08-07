#!/usr/bin/env python3
from pathlib import Path
import sys
from msoffcrypto.format.ooxml import OOXMLFile

source, target = map(Path, sys.argv[1:3])
with source.open("rb") as input_file, target.open("wb") as output_file:
    OOXMLFile(input_file).encrypt("anydoc-test-password", output_file)
print(target)
