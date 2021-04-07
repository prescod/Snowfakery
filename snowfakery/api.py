from pathlib import Path
from contextlib import contextmanager, ExitStack
import typing as T
import sys

import yaml
from click.utils import LazyFile

from snowfakery.data_generator import generate

from snowfakery.output_streams import (
    DebugOutputStream,
    SqlDbOutputStream,
    SqlTextOutputStream,
    JSONOutputStream,
    CSVOutputStream,
    ImageOutputStream,
    GraphvizOutputStream,
    MultiplexOutputStream,
)
from snowfakery.generate_mapping_from_recipe import mapping_from_recipe_templates
from snowfakery.salesforce import create_cci_record_type_tables
from snowfakery.cci_mapping_files.declaration_parser import (
    SObjectRuleDeclarationFile,
    unify,
)
import snowfakery.data_gen_exceptions as exc
from snowfakery.data_generator_runtime import (
    FinishedChecker,
    StoppingCriteria,
)
from snowfakery.data_generator_runtime import IsFinished  # noQA

OpenFileLike = T.Union[T.TextIO, LazyFile]
FileLike = T.Union[OpenFileLike, Path, str]


class EmbeddingContext:
    """Base class for all applications which embed Snowfakery as a library,
    including the Snowfakery CLI and CumulusCI"""

    stopping_criteria = None

    def __init__(self, stopping_criteria: StoppingCriteria = None):
        self.stopping_criteria = stopping_criteria
        self.finished_checker = FinishedChecker(self.stopping_criteria)

    def echo(self, message=None, file=None, nl=True, err=False, color=None):
        import click

        click.echo(message, file, nl, err, color)

    def stopping_tablename(self):
        if self.stopping_criteria:
            return self.stopping_criteria.tablename

    def check_if_finished(self, id_manager):
        return self.finished_checker.check_if_finished(id_manager)


graphic_file_extensions = [
    "PNG",
    "png",
    "SVG",
    "svg",
    "svgz",
    "jpeg",
    "jpg",
    "ps",
    "dot",
]

file_extensions = [
    "JSON",
    "json",
    "txt",
    "csv",
    "sql",
] + graphic_file_extensions


def stopping_criteria_from_target_number(target_number):
    "Deconstruct a tuple of 'str number' or 'number str' and make a StoppingCriteria"

    # 'number str' is the official format so the other one can be deprecated one day.
    if target_number:
        if isinstance(target_number[0], int):
            target_number = target_number[1], target_number[0]
        return StoppingCriteria(*target_number)

    return None


# Entry point to Snowfakery used by both the API ("snowfakery.generate_data")
# and the command line ("snowfakery.cli")
def generate_data(
    yaml_file: FileLike,
    *,
    embedding_context: EmbeddingContext = None,
    user_options: T.Dict[str, str] = None,
    dburls=[],
    dburl: str = None,
    target_number: T.Tuple = None,
    debug_internals: bool = None,
    generate_cci_mapping_file: FileLike = None,
    output_format: str = None,
    output_file: FileLike = None,
    output_files: T.List[FileLike] = None,
    output_folder: FileLike = None,
    continuation_file: FileLike = None,
    generate_continuation_file: FileLike = None,
    should_create_cci_record_type_tables: bool = False,
    load_declarations: T.Sequence[FileLike] = None,
) -> None:
    stopping_criteria = stopping_criteria_from_target_number(target_number)
    dburls = dburls or ([dburl] if dburl else [])
    output_files = output_files or []
    if output_file:
        output_files = output_files + [output_file]

    with ExitStack() as exit_stack:

        def open_with_cleanup(file, mode):
            return exit_stack.enter_context(open_file_like(file, mode))

        embedding_context = embedding_context or EmbeddingContext(stopping_criteria)

        output_stream = exit_stack.enter_context(
            configure_output_stream(
                dburls, output_format, output_files, output_folder, embedding_context
            )
        )

        yaml_path, open_yaml_file = open_with_cleanup(yaml_file, "r")
        _, open_new_continue_file = open_with_cleanup(generate_continuation_file, "w")
        _, open_continuation_file = open_with_cleanup(continuation_file, "r")
        _, open_cci_mapping_file = open_with_cleanup(generate_cci_mapping_file, "w")

        summary = generate(
            open_yaml_file=open_yaml_file,
            user_options=user_options,
            output_stream=output_stream,
            embedding_context=embedding_context,
            generate_continuation_file=open_new_continue_file,
            continuation_file=open_continuation_file,
            stopping_criteria=stopping_criteria,
        )

        # This feature seems seldom useful. Delete it if it isn't missed
        # by fall 2021:

        # if debug_internals:
        #     debuginfo = yaml.dump(summary.summarize_for_debugging(), sort_keys=False)
        #     sys.stderr.write(debuginfo)

        if open_cci_mapping_file:
            declarations = gather_declarations(yaml_path or "", load_declarations)
            yaml.safe_dump(
                mapping_from_recipe_templates(summary, declarations),
                open_cci_mapping_file,
                sort_keys=False,
            )
    if should_create_cci_record_type_tables:
        create_cci_record_type_tables(dburls[0])


@contextmanager
def configure_output_stream(
    dburls, output_format, output_files, output_folder, embedding_context
):
    assert isinstance(output_files, (list, type(None)))

    with _get_output_streams(
        dburls, output_files, output_format, output_folder
    ) as output_streams:
        if len(output_streams) == 0:
            output_stream = DebugOutputStream()
        elif len(output_streams) == 1:
            output_stream = output_streams[0]
        else:
            output_stream = MultiplexOutputStream(output_streams)
        try:
            yield output_stream
        finally:
            try:
                messages = output_stream.close()
            except Exception as e:
                messages = None
                embedding_context.echo(
                    f"Could not close {output_stream}: {str(e)}", err=True
                )
            if messages:
                for message in messages:
                    embedding_context.echo(message)


@contextmanager
def _get_output_streams(dburls, output_files, output_format, output_folder):
    with ExitStack() as onexit:
        output_streams = []  # we allow multiple output streams
        for dburl in dburls:
            output_streams.append(SqlDbOutputStream.from_url(dburl))

        # JSON and SQL are the only output formats (other than debug) that can go on stdout
        if output_format == "json" and not output_files:
            output_streams.append(JSONOutputStream(sys.stdout))

        if output_format == "sql" and not output_files:
            output_streams.append(SqlTextOutputStream(sys.stdout))

        if output_format == "csv":
            output_streams.append(CSVOutputStream(output_folder))

        if output_files:
            for f in output_files:
                if output_folder and isinstance(f, (str, Path)):
                    f = Path(output_folder, f)  # put the file in the output folder
                file_context = open_file_like(f, "w")
                path, open_file = onexit.enter_context(file_context)
                if output_format:
                    format = output_format
                elif path:
                    format = output_format or Path(path).suffix[1:]
                else:
                    raise exc.DataGenError("No format supplied or inferrable")

                if format == "json":
                    output_streams.append(JSONOutputStream(open_file))
                elif format == "sql":
                    output_streams.append(SqlTextOutputStream(open_file))
                elif format == "txt":
                    output_streams.append(DebugOutputStream(open_file))
                elif format == "dot":
                    output_streams.append(GraphvizOutputStream(open_file))
                elif format in graphic_file_extensions:
                    output_streams.append(ImageOutputStream(open_file, format))
                else:
                    raise exc.DataGenError(
                        f"Unknown format or file extension: {format}"
                    )
        yield output_streams


def gather_declarations(yaml_file, load_declarations):
    """Gather declarations from load declaration files."""
    if not load_declarations:
        inferred_load_file_path = infer_load_file_path(yaml_file)
        if inferred_load_file_path.is_file():
            load_declarations = [inferred_load_file_path]

    if load_declarations:
        declarations = []
        for declfile in load_declarations:
            with open_file_like(declfile, "r") as (path, f):
                declarations.extend(SObjectRuleDeclarationFile.parse_from_yaml(f))

        unified_declarations = unify(declarations)
    else:
        unified_declarations = {}
    return unified_declarations


def infer_load_file_path(yaml_file: T.Union[str, Path]):
    """Infer a load declaration from a filename"""
    yaml_file = str(yaml_file)
    suffixes = "".join(Path(yaml_file).suffixes)
    if suffixes:
        return Path(yaml_file.replace(suffixes, ".load.yml"))
    else:
        return Path("")


@contextmanager
def open_file_like(
    file_like: T.Optional[FileLike], mode
) -> T.ContextManager[T.Tuple[str, OpenFileLike]]:
    if not file_like:
        yield None, None
    if isinstance(file_like, str):
        file_like = Path(file_like)

    if isinstance(file_like, Path):
        with file_like.open(mode) as f:
            yield file_like, f

    elif hasattr(file_like, "name"):
        yield file_like.name, file_like

    elif hasattr(file_like, "read"):
        yield None, file_like
