import atexit
import csv
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import requests
from rich import progress
from rich.logging import RichHandler
from rich.highlighter import NullHighlighter
from rich.panel import Panel
from rich.console import Console
from rich.progress import Progress, TextColumn, BarColumn
from functools import partial
from glob import glob
from os.path import dirname, exists, isdir, join
from packaging import version
from statistics import mean, median
from tqdm import tqdm
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np
import slideflow as sf
from slideflow.util import log_utils
from slideflow import errors
from slideflow.util import example_pb2
from slideflow.util.colors import *  # noqa F403,F401 - Here for compatibility

tf_available = importlib.util.find_spec('tensorflow')
torch_available = importlib.util.find_spec('torch')

# Enable color sequences on Windows
try:
    import ctypes.windll
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
except Exception:
    pass


# --- Global vars -------------------------------------------------------------

SUPPORTED_FORMATS = ['svs', 'tif', 'ndpi', 'vms', 'vmu', 'scn', 'mrxs',
                     'tiff', 'svslide', 'bif', 'jpg', 'jpeg']
EMPTY_ANNOTATIONS = ['', ' ']
CPLEX_AVAILABLE = (importlib.util.find_spec('cplex') is not None)
try:
    import pyomo.environ as pyo
    from pyomo.opt import SolverFactory
    opt = SolverFactory('bonmin', validate=False)
    if not opt.available():
        raise errors.SolverNotFoundError
except Exception:
    BONMIN_AVAILABLE = False
else:
    BONMIN_AVAILABLE = True


# --- Commonly used types -----------------------------------------------------

# Outcome labels
Labels = Union[Dict[str, str], Dict[str, int], Dict[str, List[float]]]

# Normalizer fit keyword arguments
NormFit = Union[Dict[str, np.ndarray], Dict[str, List]]

# --- Configure logging--------------------------------------------------------
log = logging.getLogger('slideflow')
log.setLevel(logging.DEBUG)


def setLoggingLevel(level):
    log.handlers[0].setLevel(level)


def getLoggingLevel():
    return log.handlers[0].level


def addLoggingFileHandler(path):
    fh = logging.FileHandler(path)
    fh.setFormatter(log_utils.FileFormatter())
    handler = log_utils.MultiProcessingHandler(
        "mp-file-handler-{0}".format(len(log.handlers)),
        sub_handler=fh
    )
    log.addHandler(handler)
    atexit.register(handler.close)


# Add tqdm-friendly stream handler
#ch = log_utils.TqdmLoggingHandler()
ch = RichHandler(markup=True, log_time_format="[%X]", show_path=False, highlighter=NullHighlighter(), rich_tracebacks=True)
ch.setFormatter(log_utils.LogFormatter())
if 'SF_LOGGING_LEVEL' in os.environ:
    try:
        intLevel = int(os.environ['SF_LOGGING_LEVEL'])
        ch.setLevel(intLevel)
    except ValueError:
        pass
else:
    ch.setLevel(logging.INFO)
log.addHandler(ch)

# Add multiprocessing-friendly file handler
addLoggingFileHandler("slideflow.log")

# Workaround for duplicate logging with TF 2.9
log.propagate = False


class TileExtractionSpeedColumn(progress.ProgressColumn):
    """Renders human readable transfer speed."""

    def render(self, task: "progress.Task") -> progress.Text:
        """Show data transfer speed."""
        speed = task.finished_speed or task.speed
        if speed is None:
            return progress.Text("?", style="progress.data.speed")
        data_speed = f'{int(speed)} img'
        return progress.Text(f"{data_speed}/s", style="progress.data.speed")

class ImgBatchSpeedColumn(progress.ProgressColumn):
    """Renders human readable transfer speed."""

    def __init__(self, batch_size=1, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.batch_size = batch_size

    def render(self, task: "progress.Task") -> progress.Text:
        """Show data transfer speed."""
        speed = task.finished_speed or task.speed
        if speed is None:
            return progress.Text("?", style="progress.data.speed")
        data_speed = f'{int(speed * self.batch_size)} img'
        return progress.Text(f"{data_speed}/s", style="progress.data.speed")


class TileExtractionProgress(Progress):
    def get_renderables(self):
        for task in self.tasks:
            if task.fields.get("progress_type") == 'speed':
                self.columns = (
                    TextColumn("[progress.description]{task.description}"),
                    TileExtractionSpeedColumn(),)
            if task.fields.get("progress_type") == 'slide_progress':
                self.columns = (
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    progress.TaskProgressColumn(),
                    progress.MofNCompleteColumn(),
                    "●",
                    progress.TimeRemainingColumn(),
                )
            yield self.make_tasks_table([task])


# --- Slideflow header --------------------------------------------------------

def header(console=None):
    if console is None:
        console = Console()
    color = 'yellow' if sf.backend() == 'tensorflow' else 'purple'
    console.print(
        Panel(f"[white bold]Slideflow[/]"
              f"\nVersion: {sf.__version__}"
              f"\nBackend: [{color}]{sf.backend()}[/]"
              "\n[blue]https://slideflow.dev[/]",
              border_style='purple'),
        justify='center')


# --- Data download functions -------------------------------------------------

def download_from_tcga(
    uuid: str,
    dest: str,
    message: str = 'Downloading...'
) -> None:
    """Download a file from TCGA (GDC) by UUID."""
    data_endpt = f"https://api.gdc.cancer.gov/data/"
    response = requests.post(
        data_endpt,
        data=json.dumps({'ids': [uuid]}),
        headers={"Content-Type": "application/json"},
        stream=True
    )
    response_head_cd = response.headers["Content-Disposition"]
    block_size = 4096
    block_per_mb = block_size / 1000000
    file_size = int(response.headers.get('Content-Length', ''))
    file_size_mb = file_size / 1000000
    running_total_mb = 0
    file_name = join(dest, re.findall("filename=(.+)", response_head_cd)[0])
    pbar = tqdm(desc=message,
                total=file_size_mb, unit='MB',
                bar_format="{desc}: {percentage:3.0f}%|{bar}| "
                           "{n:.2f}/{total:.2f} [{elapsed}<{remaining}] "
                           "{rate_fmt}{postfix}")

    with open(file_name, "wb") as output_file:
        for chunk in response.iter_content(chunk_size=block_size):
            output_file.write(chunk)
            if block_per_mb + running_total_mb < file_size_mb:
                running_total_mb += block_per_mb  # type: ignore
                pbar.update(block_per_mb)
            else:
                running_total_mb += file_size_mb - running_total_mb  # type: ignore
                pbar.update(file_size_mb - running_total_mb)

# --- Utility functions and classes -------------------------------------------

def model_backend(model):
    if sf.util.torch_available and 'torch' in sys.modules:
        import torch
        if isinstance(model, torch.nn.Module):
            return 'torch'
    if sf.util.tf_available and 'tensorflow' in sys.modules:
        import tensorflow as tf
        if isinstance(model, tf.keras.Model):
            return 'tensorflow'
        from tensorflow.lite.python.interpreter import SignatureRunner
        if isinstance(model, SignatureRunner):
            return 'tflite'
    raise ValueError(f"Unable to interpret model {model}")

def detuple(arg1: Any, args: tuple) -> Any:
    if len(args):
        return tuple([arg1] + list(args))
    else:
        return arg1


def batch(iterable: List, n: int = 1) -> Iterable:
    """Separates an interable into batches of maximum size `n`."""
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx:min(ndx + n, l)]


def as_list(arg1: Any) -> List[Any]:
    if not isinstance(arg1, list):
        return [arg1]
    else:
        return arg1


def is_mag(arg1: str) -> bool:
    arg1_split = arg1.lower().split('x')
    if (len(arg1_split) != 2) or (arg1_split[1] != ''):
        return False
    try:
        mag = float(arg1_split[0])
    except ValueError:
        return False
    return True


def is_model(path: str) -> bool:
    """Checks if the given path is a valid Slideflow model."""
    return is_tensorflow_model_path(path) or is_torch_model_path(path)


def is_project(path: str) -> bool:
    """Checks if the given path is a valid Slideflow project."""
    return isdir(path) and exists(join(path, 'settings.json'))


def is_slide(path: str) -> bool:
    """Checks if the given path is a supported slide."""
    return (os.path.isfile(path)
            and sf.util.path_to_ext(path).lower() in SUPPORTED_FORMATS)


def is_tensorflow_model_path(path: str) -> bool:
    """Checks if the given path is a valid Slideflow/Tensorflow model."""
    return (isdir(path)
            and (exists(join(path, 'params.json'))
                 or exists(join(dirname(path), 'params.json'))))


def is_torch_model_path(path: str) -> bool:
    """Checks if the given path is a valid Slideflow/PyTorch model."""
    return (os.path.isfile(path)
            and sf.util.path_to_ext(path).lower() == 'zip'
            and exists(join(dirname(path), 'params.json')))


def assert_is_mag(arg1: str):
    if not isinstance(arg1, str) or not is_mag(arg1):
        raise ValueError(
            f'Invalid magnification {arg1}. Must be of format'
            f' [int/float]x, such as "10x", "20X", or "2.5x"'
        )


def to_mag(arg1: str) -> Union[int, float]:
    assert_is_mag(arg1)
    try:
        return int(arg1.lower().split('x')[0])
    except ValueError:
        return float(arg1.lower().split('x')[0])


def multi_warn(arr: List, compare: Callable, msg: Union[Callable, str]) -> int:
    """Logs multiple warning

    Args:
        arr (List): Array to compare.
        compare (Callable): Comparison to perform on array. If True, will warn.
        msg (str): Warning message.

    Returns:
        int: Number of warnings.
    """
    num_warned = 0
    warn_threshold = 3
    for item in arr:
        if compare(item):
            fn = log.warn if num_warned < warn_threshold else log.debug
            if isinstance(msg, str):
                fn(msg.format(item))
            elif callable(msg):
                fn(msg(item))
            num_warned += 1
    if num_warned >= warn_threshold:
        log.warn(f'...{num_warned} total warnings, see log for details')
    return num_warned


def to_onehot(val: int, max: int) -> np.ndarray:
    """Converts value to one-hot encoding

    Args:
        val (int): Value to encode
        max (int): Maximum value (length of onehot encoding)
    """

    onehot = np.zeros(max, dtype=np.int64)
    onehot[val] = 1
    return onehot


def clear_console() -> None:
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def make_dir(_dir: str) -> None:
    """Makes a directory if one does not already exist,
    in a manner compatible with multithreading.
    """
    if not exists(_dir):
        try:
            os.makedirs(_dir, exist_ok=True)
        except FileExistsError:
            pass


def relative_path(path: str, root: str):
    """Returns a relative path, from a given root directory."""
    if path[0] == '.':
        return join(root, path[2:])
    elif path.startswith('$ROOT'):
        raise ValueError("Invalid path prefix $ROOT; update project settings")
    else:
        return path


def global_path(root: str, path_string: str):
    '''Returns global path from a local path.'''
    if not root:
        root = ""
    if path_string and (len(path_string) > 2) and path_string[:2] == "./":
        return os.path.join(root, path_string[2:])
    elif path_string and (path_string[0] != "/"):
        return os.path.join(root, path_string)
    else:
        return path_string


def _shortname(string: str):
    if len(string) == 60:
        # May be TCGA slide with long name; convert to
        # patient name by returning first 12 characters
        return string[:12]
    else:
        return string


def yes_no_input(prompt: str, default: str = 'no') -> bool:
    '''Prompts user for yes/no input.'''
    while True:
        response = input(prompt)
        if not response and default:
            return (default in ('yes', 'y'))
        elif response.lower() in ('yes', 'no', 'y', 'n'):
            return (response.lower() in ('yes', 'y'))
        else:
            print("Invalid response.")


def path_input(
    prompt: str,
    root: str,
    default: Optional[str] = None,
    create_on_invalid: bool = False,
    filetype: Optional[str] = None,
    verify: bool = True
) -> str:
    '''Prompts user for directory input.'''
    while True:
        relative_response = input(f"{prompt}")
        reponse = global_path(root, relative_response)
        if not relative_response and default:
            relative_response = default
            reponse = global_path(root, relative_response)
        if verify and not os.path.exists(reponse):
            if not filetype and create_on_invalid:
                prompt = f'Path "{reponse}" does not exist. Create? [Y/n] '
                if yes_no_input(prompt, default='yes'):
                    os.makedirs(reponse)
                    return relative_response
                else:
                    continue
            elif filetype:
                print(f'Unable to locate file "{reponse}"')
                continue
        elif not filetype and not os.path.exists(reponse):
            print(f'Unable to locate directory "{reponse}"')
            continue
        resp_type = path_to_ext(reponse)
        if filetype and (resp_type != filetype):
            print(f'Incorrect filetype "{resp_type}", expected "{filetype}"')
            continue
        return relative_response


def choice_input(prompt, valid_choices, default=None, multi_choice=False,
                 input_type=str):
    '''Prompts user for multi-choice input.'''
    while True:
        response = input(f"{prompt}")
        if not response and default:
            return default
        if not multi_choice and response not in valid_choices:
            print("Invalid option.")
            continue
        elif multi_choice:
            try:
                replaced = response.replace(" ", "")
                response = [input_type(r) for r in replaced.split(',')]
            except ValueError:
                print(f"Invalid selection (response: {response})")
                continue
            invalid = [r not in valid_choices for r in response]
            if any(invalid):
                print(f'Invalid selection (response: {response})')
                continue
        return response


def load_json(filename: str) -> Any:
    '''Reads JSON data from file.'''
    with open(filename, 'r') as data_file:
        return json.load(data_file)


def write_json(data: Any, filename: str) -> None:
    '''Writes data to JSON file.'''
    with open(filename, "w") as data_file:
        json.dump(data, data_file, indent=1)


def get_slides_from_model_manifest(
    model_path: str,
    dataset: Optional[str] = None
) -> List[str]:
    """Get list of slides from a model manifest.

    Args:
        model_path (str): Path to model from which to load the model manifest.
        dataset (str):  'training' or 'validation'. Will return only slides
            from this dataset. Defaults to None (all).

    Returns:
        list(str): List of slide names.
    """

    slides = []
    if exists(join(model_path, 'slide_manifest.csv')):
        manifest = join(model_path, 'slide_manifest.csv')
    elif exists(join(dirname(model_path), 'slide_manifest.csv')):
        log.debug("Slide manifest not found in model directory")
        log.debug("Loading manifest from parent directory.")
        manifest = join(dirname(model_path), 'slide_manifest.csv')
    else:
        log.error('Slide manifest not found in model folder')
        return []
    with open(manifest, 'r') as manifest_file:
        reader = csv.reader(manifest_file)
        header = next(reader)
        dataset_index = header.index('dataset')
        slide_index = header.index('slide')
        for row in reader:
            dataset_name = row[dataset_index]
            slide_name = row[slide_index]
            if dataset_name == dataset or not dataset:
                slides += [slide_name]
    return slides


def get_gan_config(model_path: str) -> Dict:
    """Loads a GAN training_options.json for an associated network PKL."""

    if exists(join(dirname(model_path), 'training_options.json')):
        return load_json(join(dirname(model_path), 'training_options.json'))
    else:
        raise errors.ModelParamsNotFoundError


def get_model_config(model_path: str) -> Dict:
    """Loads model configuration JSON file."""

    if exists(join(model_path, 'params.json')):
        config = load_json(join(model_path, 'params.json'))
    elif exists(join(dirname(model_path), 'params.json')):
        if not (sf.util.torch_available
                and sf.util.path_to_ext(model_path) == 'zip'):
            log.warning(
                "Hyperparameters not in model directory; loading from parent"
                " directory. Please move params.json into model folder."
            )
        config = load_json(join(dirname(model_path), 'params.json'))
    else:
        raise errors.ModelParamsNotFoundError
    # Compatibility for pre-1.1
    if 'norm_mean' in config:
        config['norm_fit'] = {
            'target_means': config['norm_mean'],
            'target_stds': config['norm_std'],
        }
    if 'outcome_label_headers' in config:
        log.debug("Replacing outcome_label_headers in params.json -> outcomes")
        config['outcomes'] = config.pop('outcome_label_headers')
    return config


def get_model_normalizer(
    model_path: str
) -> Optional["sf.norm.StainNormalizer"]:
    """Loads and fits normalizer using configuration at a model path."""

    config = sf.util.get_model_config(model_path)

    if not config['hp']['normalizer']:
        return None

    if ('slideflow_version' in config
       and version.parse(config['slideflow_version']) <= version.parse("1.2.2")
       and config['hp']['normalizer'] in ('vahadane', 'macenko')):
        log.warn("Detected model trained with Macenko or Vahadane "
                    "normalization with Slideflow version <= 1.2.2. Macenko "
                    "and Vahadane algorithms were optimized in 1.2.3 and may "
                    "now yield slightly different results. ")

    normalizer = sf.norm.autoselect(
        config['hp']['normalizer'],
        config['hp']['normalizer_source']
    )
    if 'norm_fit' in config and config['norm_fit'] is not None:
        normalizer.set_fit(**config['norm_fit'])
    return normalizer


def get_slide_paths(slides_dir: str) -> List[str]:
    '''Get all slide paths from a given directory containing slides.'''
    slide_list = [i for i in glob(join(slides_dir, '**/*.*')) if is_slide(i)]
    slide_list.extend([i for i in glob(join(slides_dir, '*.*')) if is_slide(i)])
    return slide_list


def read_annotations(path: str) -> Tuple[List[str], List[Dict]]:
    '''Read an annotations file.'''
    results = []
    with open(path, 'r') as csv_file:
        csv_reader = csv.reader(csv_file, delimiter=',')
        # First, try to open file
        try:
            header = next(csv_reader, None)
        except OSError:
            raise OSError(
                f"Failed to open annotations file {path}"
            )
        assert isinstance(header, list)
        for row in csv_reader:
            row_dict = {}
            for i, key in enumerate(header):
                row_dict[key] = row[i]
            results += [row_dict]
    return header, results


def get_relative_tfrecord_paths(root: str, directory: str = "") -> List[str]:
    '''Returns relative tfrecord paths with respect to the given directory.'''

    tfrecords = [
        join(directory, f) for f in os.listdir(join(root, directory))
        if (not isdir(join(root, directory, f))
            and len(f) > 10 and f[-10:] == ".tfrecords")
    ]
    subdirs = [
        f for f in os.listdir(join(root, directory))
        if isdir(join(root, directory, f))
    ]
    for sub in subdirs:
        tfrecords += get_relative_tfrecord_paths(root, join(directory, sub))
    return tfrecords


def contains_nested_subdirs(directory: str) -> bool:
    subdirs = [
        _dir for _dir in os.listdir(directory)
        if isdir(join(directory, _dir))
    ]
    for subdir in subdirs:
        contents = os.listdir(join(directory, subdir))
        for c in contents:
            if isdir(join(directory, subdir, c)):
                return True
    return False


def path_to_name(path: str) -> str:
    '''Returns name of a file, without extension,
    from a given full path string.'''
    _file = path.split('/')[-1]
    if len(_file.split('.')) == 1:
        return _file
    else:
        return '.'.join(_file.split('.')[:-1])


def path_to_ext(path: str) -> str:
    '''Returns extension of a file path string.'''
    _file = path.split('/')[-1]
    if len(_file.split('.')) == 1:
        return ''
    else:
        return _file.split('.')[-1]


def update_results_log(
    results_log_path: str,
    model_name: str,
    results_dict: Dict
) -> None:
    '''Dynamically update results_log when recording training metrics.'''
    # First, read current results log into a dictionary
    results_log = {}  # type: Dict[str, Any]
    if exists(results_log_path):
        with open(results_log_path, "r") as results_file:
            reader = csv.reader(results_file)
            try:
                headers = next(reader)
            except StopIteration:
                pass
            else:
                try:
                    model_name_i = headers.index('model_name')
                    result_keys = [k for k in headers if k != 'model_name']
                except ValueError:
                    model_name_i = headers.index('epoch')
                    result_keys = [k for k in headers if k != 'epoch']
                for row in reader:
                    name = row[model_name_i]
                    results_log[name] = {}
                    for result_key in result_keys:
                        result = row[headers.index(result_key)]
                        results_log[name][result_key] = result
        # Move the current log file into a temporary file
        shutil.move(results_log_path, f"{results_log_path}.temp")

    # Next, update the results log with the new results data
    for epoch in results_dict:
        results_log.update({f'{model_name}-{epoch}': results_dict[epoch]})

    # Finally, create a new log file incorporating the new data
    with open(results_log_path, "w") as results_file:
        writer = csv.writer(results_file)
        result_keys = []
        # Search through results to find all results keys
        for model in results_log:
            result_keys += list(results_log[model].keys())
        # Remove duplicate result keys
        result_keys = list(set(result_keys))
        result_keys.sort()
        # Write header labels
        writer.writerow(['model_name'] + result_keys)
        # Iterate through model results and record
        for model in results_log:
            row = [model]
            # Include all saved metrics
            for result_key in result_keys:
                if result_key in results_log[model]:
                    row += [results_log[model][result_key]]
                else:
                    row += [""]
            writer.writerow(row)

    # Delete the old results log file
    if exists(f"{results_log_path}.temp"):
        os.remove(f"{results_log_path}.temp")


def tfrecord_heatmap(
    tfrecord: str,
    slide: str,
    tile_px: int,
    tile_um: Union[int, str],
    tile_dict: Dict[int, float],
    outdir: str,
    interpolation: Optional[str] = 'bicubic'
) -> Dict[str, Dict[str, float]]:
    """Creates a tfrecord-based WSI heatmap using a dictionary of tile values
    for heatmap display.

    Args:
        tfrecord (str): Path to tfrecord.
        slide (str): Path to whole-slide image.
        tile_dict (dict): Dictionary mapping tfrecord indices to a
            tile-level value for display in heatmap format.
        tile_px (int): Tile width in pixels.
        tile_um (int or str): Tile width in microns (int) or magnification
            (str, e.g. "20x").
        outdir (str): Path to directory in which to save images.

    Returns:
        Dictionary mapping slide names to dict of statistics
        (mean, median, above_0, and above_1)
    """
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcol

    slide_name = sf.util.path_to_name(tfrecord)
    loc_dict = sf.io.get_locations_from_tfrecord(tfrecord)
    if tile_dict.keys() != loc_dict.keys():
        td_len = len(list(tile_dict.keys()))
        loc_len = len(list(loc_dict.keys()))
        raise errors.TFRecordsError(
            f'tile_dict length ({td_len}) != TFRecord length ({loc_len}).'
        )

    log.info(f'Generating TFRecord heatmap for [green]{tfrecord}[/]...')
    wsi = sf.slide.WSI(slide, tile_px, tile_um)

    stats = {}

    # Loaded CSV coordinates:
    x = [int(loc_dict[loc][0]) for loc in loc_dict]
    y = [int(loc_dict[loc][1]) for loc in loc_dict]
    vals = [tile_dict[loc] for loc in loc_dict]

    stats.update({
        slide_name: {
            'mean': mean(vals),
            'median': median(vals),
            'above_0': len([v for v in vals if v > 0]),
            'above_1': len([v for v in vals if v > 1]),
        }
    })

    log.debug('Loaded tile values')
    log.debug(f'Min: {min(vals)}\t Max:{max(vals)}')

    scaled_x = [(xi * wsi.roi_scale) - wsi.full_extract_px/2 for xi in x]
    scaled_y = [(yi * wsi.roi_scale) - wsi.full_extract_px/2 for yi in y]

    log.debug('Loaded CSV coordinates:')
    log.debug(f'Min x: {min(x)}\t Max x: {max(x)}')
    log.debug(f'Min y: {min(y)}\t Max y: {max(y)}')
    log.debug('Scaled CSV coordinates:')
    log.debug(f'Min x: {min(scaled_x)}\t Max x: {max(scaled_x)}')
    log.debug(f'Min y: {min(scaled_y)}\t Max y: {max(scaled_y)}')
    log.debug('Slide properties:')
    log.debug(f'Size (x): {wsi.dimensions[0]}\t Size (y): {wsi.dimensions[1]}')

    # Slide coordinate information
    max_coord_x = max([c[0] for c in wsi.coord])
    max_coord_y = max([c[1] for c in wsi.coord])
    num_x = len(set([c[0] for c in wsi.coord]))
    num_y = len(set([c[1] for c in wsi.coord]))

    log.debug('Slide tile grid:')
    log.debug(f'Number of tiles (x): {num_x}\t Max coord (x): {max_coord_x}')
    log.debug(f'Number of tiles (y): {num_y}\t Max coord (y): {max_coord_y}')

    # Calculate dead space (un-extracted tiles) in x and y axes
    dead_x = wsi.dimensions[0] - max_coord_x
    dead_y = wsi.dimensions[1] - max_coord_y
    fraction_dead_x = dead_x / wsi.dimensions[0]
    fraction_dead_y = dead_y / wsi.dimensions[1]

    log.debug('Slide dead space')
    log.debug(f'x: {dead_x}\t y:{dead_y}')

    # Work on grid
    x_grid_scale = max_coord_x / (num_x-1)
    y_grid_scale = max_coord_y / (num_y-1)

    log.debug('Coordinate grid scale:')
    log.debug(f'x: {x_grid_scale}\t y: {y_grid_scale}')

    grid = np.zeros((num_y, num_x))
    indexed_x = [round(xi / x_grid_scale) for xi in scaled_x]
    indexed_y = [round(yi / y_grid_scale) for yi in scaled_y]

    for xi, yi, v in zip(indexed_x, indexed_y, vals):
        grid[yi][xi] = v

    fig = plt.figure(figsize=(18, 16))
    ax = fig.add_subplot(111)
    fig.subplots_adjust(bottom=0.25, top=0.95)
    gca = plt.gca()
    gca.tick_params(
        axis='x',
        top=True,
        labeltop=True,
        bottom=False,
        labelbottom=False
    )
    log.info('Generating thumbnail...')
    thumb = wsi.thumb(mpp=5)
    log.info('Saving thumbnail....')
    thumb.save(join(outdir, f'{slide_name}' + '.png'))
    log.info('Generating figure...')
    implot = ax.imshow(thumb, zorder=0)
    extent = implot.get_extent()
    extent_x = extent[1] * (1-fraction_dead_x)
    extent_y = extent[2] * (1-fraction_dead_y)
    grid_extent = (extent[0], extent_x, extent_y, extent[3])
    log.debug('\nImage extent:')
    log.debug(extent)
    log.debug('\nGrid extent:')
    log.debug(grid_extent)

    divnorm = mcol.TwoSlopeNorm(
        vmin=min(-0.01, min(vals)),
        vcenter=0,
        vmax=max(0.01, max(vals))
    )
    ax.imshow(
        grid,
        zorder=10,
        alpha=0.6,
        extent=grid_extent,
        interpolation=interpolation,
        cmap='coolwarm',
        norm=divnorm
    )
    log.info('Saving figure...')
    plt.savefig(join(outdir, f'{slide_name}_attn.png'), bbox_inches='tight')
    log.debug('Cleaning up...')
    plt.clf()
    del wsi
    del thumb
    return stats


def get_new_model_dir(root: str, model_name: str) -> str:
    prev_run_dirs = [
        x for x in os.listdir(root)
        if isdir(join(root, x))
    ]
    prev_run_ids = [re.match(r'^\d+', x) for x in prev_run_dirs]  # type: List
    prev_run_ids = [int(x.group()) for x in prev_run_ids if x is not None]
    cur_id = max(prev_run_ids, default=-1) + 1
    model_dir = os.path.join(root, f'{cur_id:05d}-{model_name}')
    assert not os.path.exists(model_dir)
    os.makedirs(model_dir)
    return model_dir


def split_list(a: List, n: int) -> List[List]:
    '''Function to split a list into n components'''
    k, m = divmod(len(a), n)
    return [a[i * k + min(i, m): (i + 1) * k + min(i + 1, m)]
            for i in range(n)]


# --- TFRecord utility functions ----------------------------------------------

def process_feature(
    feature: example_pb2.Feature,  # type: ignore
    typename: str,
    typename_mapping: Dict,
    key: str
) -> np.ndarray:
    # NOTE: We assume that each key in the example has only one field
    # (either "bytes_list", "float_list", or "int64_list")!
    field = feature.ListFields()[0]  # type: ignore
    inferred_typename, value = field[0].name, field[1].value

    if typename is not None:
        tf_typename = typename_mapping[typename]
        if tf_typename != inferred_typename:
            reversed_mapping = {v: k for k, v in typename_mapping.items()}
            raise TypeError(
                f"Incompatible type '{typename}' for `{key}` "
                f"(should be '{reversed_mapping[inferred_typename]}')."
            )

    if inferred_typename == "bytes_list":
        value = np.frombuffer(value[0], dtype=np.uint8)
    elif inferred_typename == "float_list":
        value = np.array(value, dtype=np.float32)
    elif inferred_typename == "int64_list":
        value = np.array(value, dtype=np.int64)
    return value


def extract_feature_dict(
    features: Union[example_pb2.FeatureLists,  # type: ignore
                    example_pb2.Features],  # type: ignore
    description: Optional[Union[List, Dict]],
    typename_mapping: Dict
) -> Dict[str, Any]:
    if isinstance(features, example_pb2.FeatureLists):
        features = features.feature_list  # type: ignore

        def get_value(typename, typename_mapping, key):
            feature = features[key].feature
            fn = partial(
                process_feature,
                typename=typename,
                typename_mapping=typename_mapping,
                key=key
            )
            return list(map(fn, feature))
    elif isinstance(features, example_pb2.Features):
        features = features.feature  # type: ignore

        def get_value(typename, typename_mapping, key):
            return process_feature(features[key], typename,
                                   typename_mapping, key)
    else:
        raise TypeError(f"Incompatible type: features should be either of type "
                        f"example_pb2.Features or example_pb2.FeatureLists and "
                        f"not {type(features)}")

    all_keys = list(features.keys())  # type: ignore

    if description is None or len(description) == 0:
        description = dict.fromkeys(all_keys, None)
    elif isinstance(description, list):
        description = dict.fromkeys(description, None)

    processed_features = {}
    for key, typename in description.items():
        if key not in all_keys:
            raise KeyError(f"Key {key} doesn't exist (select from {all_keys})!")

        processed_features[key] = get_value(typename, typename_mapping, key)

    return processed_features
