import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli.args import parse
from src.output import output_path


def test_prod_path_default():
    args = parse([])
    assert str(output_path(args)) == "prod.log"
