import functools
import inspect
from pathlib import Path
from typing import Callable, Tuple, Type

from _pytest.fixtures import FixtureRequest

from manim import Scene
from manim._config import tempconfig
from manim._config.utils import ManimConfig
from manim.camera.three_d_camera import ThreeDCamera
from manim.renderer.cairo_renderer import CairoRenderer
from manim.scene.three_d_scene import ThreeDScene

from ._frames_testers import _ControlDataWriter, _FramesTester
from ._test_class_makers import (
    DummySceneFileWriter,
    _make_scene_file_writer_class,
    _make_test_renderer_class,
    _make_test_scene_class,
)

SCENE_PARAMETER_NAME = "scene"
_tests_root_dir_path = Path(__file__).absolute().parents[2]
PATH_CONTROL_DATA = _tests_root_dir_path / Path("control_data", "graphical_units_data")


def frames_comparison(
    func=None,
    *,
    last_frame: bool = True,
    renderer_class=CairoRenderer,
    base_scene=Scene,
    **custom_config,
):
    """Compares the frames generated by the test with control frames previously registered.

    If there is no control frames for this test, the test will fail. To generate
    control frames for a given test, pass ``--set_test`` flag to pytest
    while running the test.

    Note that this decorator can be use with or without parentheses.

    Parameters
    ----------
    test_name
        The name of the test.
    module_name
        The module which the test belongs to.
    last_frame
        whether the test should test the last frame, by default True.
    renderer_class
        The base renderer to use (OpenGLRenderer/CairoRenderer), by default CairoRenderer
    base_scene
        The base class for the scene (ThreeDScene, etc.), by default Scene

    .. warning::
        By default, last_frame is True, which means that only the last frame is tested.
        If the scene has a moving animation, then the test must set last_frame to False.
    """

    def decorator_maker(tested_scene_construct):
        if (
            SCENE_PARAMETER_NAME
            not in inspect.getfullargspec(tested_scene_construct).args
        ):
            raise Exception(
                f"Invalid graphical test function test function : must have '{SCENE_PARAMETER_NAME}'as one of the parameters."
            )

        # Exclude "scene" from the argument list of the signature.
        old_sig = inspect.signature(
            functools.partial(tested_scene_construct, scene=None)
        )

        if "__module_test__" not in tested_scene_construct.__globals__:
            raise Exception(
                "There is no module test name indicated for the graphical unit test. You have to declare __module_test__ in the test file."
            )
        module_name = tested_scene_construct.__globals__.get("__module_test__")
        test_name = tested_scene_construct.__name__[len("test_") :]

        @functools.wraps(tested_scene_construct)
        # The "request" parameter is meant to be used as a fixture by pytest. See below.
        def wrapper(*args, request: FixtureRequest, tmp_path, **kwargs):
            # Wraps the test_function to a construct method, to "freeze" the eventual additional arguments (parametrizations fixtures).
            construct = functools.partial(tested_scene_construct, *args, **kwargs)

            # Kwargs contains the eventual parametrization arguments.
            # This modify the test_name so the it is defined by the parametrization arguments too.
            # Ex : if "length" is parametrized from 0 to 20, the kwargs will be with once with {"length" : 1}, etc.
            test_name_with_param = test_name + "_".join(
                map(lambda tup: f"{str(tup[0])}:{str(tup[1])}", kwargs.items())
            )

            config_tests = _config_test(last_frame)

            config_tests["text_dir"] = tmp_path
            config_tests["tex_dir"] = tmp_path

            if last_frame:
                config_tests["frame_rate"] = 1
                config_tests["dry_run"] = True

            setting_test = request.config.getoption("--set_test")
            real_test = _make_test_comparing_frames(
                file_path=_control_data_path(
                    module_name, test_name_with_param, setting_test
                ),
                base_scene=base_scene,
                construct=construct,
                renderer_class=renderer_class,
                is_set_test_data_test=setting_test,
                last_frame=last_frame,
                show_diff=request.config.getoption("--show_diff"),
                size_frame=(config_tests["pixel_height"], config_tests["pixel_width"]),
            )

            # Isolate the config used for the test, to avoid a modifying the global config during the test run.
            with tempconfig({**config_tests, **custom_config}):
                real_test()

        parameters = list(old_sig.parameters.values())
        # Adds "request" param into the signature of the wrapper, to use the associated pytest fixture.
        # This fixture is needed to have access to flags value and pytest's config. See above.
        if "request" not in old_sig.parameters:
            parameters += [inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY)]
        if "tmp_path" not in old_sig.parameters:
            parameters += [
                inspect.Parameter("tmp_path", inspect.Parameter.KEYWORD_ONLY)
            ]
        new_sig = old_sig.replace(parameters=parameters)
        wrapper.__signature__ = new_sig

        # Reach a bit into pytest internals to hoist the marks from our wrapped
        # function.
        setattr(wrapper, "pytestmark", [])
        new_marks = getattr(tested_scene_construct, "pytestmark", [])
        wrapper.pytestmark = new_marks
        return wrapper

    # Case where the decorator is called with and without parentheses.
    # If func is None, callabl(None) returns False
    if callable(func):
        return decorator_maker(func)
    return decorator_maker


def _make_test_comparing_frames(
    file_path: Path,
    base_scene: Type[Scene],
    construct: Callable[[Scene], None],
    renderer_class,  # Renderer type, there is no superclass renderer yet .....
    is_set_test_data_test: bool,
    last_frame: bool,
    show_diff: bool,
    size_frame: Tuple,
) -> Callable[[], None]:
    """Create the real pytest test that will fail if the frames mismatch.

    Parameters
    ----------
    file_path : Path
        The path of the control frames.
    base_scene : Type[Scene]
        The base scene class.
    construct : Callable[[Scene], None]
        The construct method (= the test function)
    renderer_class : [type]
        The renderer base class.
    show_diff : bool
        whether to visually show_diff (see --show_diff)

    Returns
    -------
    Callable[[], None]
        The pytest test.
    """

    if is_set_test_data_test:
        frames_tester = _ControlDataWriter(file_path, size_frame=size_frame)
    else:
        frames_tester = _FramesTester(file_path, show_diff=show_diff)

    file_writer_class = (
        _make_scene_file_writer_class(frames_tester)
        if not last_frame
        else DummySceneFileWriter
    )
    testRenderer = _make_test_renderer_class(renderer_class)

    def real_test():
        with frames_tester.testing():
            sceneTested = _make_test_scene_class(
                base_scene=base_scene,
                construct_test=construct,
                # NOTE this is really ugly but it's due to the very bad design of the two renderers.
                # If you pass a custom renderer to the Scene, the Camera class given as an argument in the Scene
                # is not passed to the renderer. See __init__ of Scene.
                # This potentially prevents OpenGL testing.
                test_renderer=testRenderer(file_writer_class=file_writer_class)
                if base_scene is not ThreeDScene
                else testRenderer(
                    file_writer_class=file_writer_class, camera_class=ThreeDCamera
                ),  # testRenderer(file_writer_class=file_writer_class),
            )
            scene_tested = sceneTested(skip_animations=True)
            scene_tested.render()
            if last_frame:
                frames_tester.check_frame(-1, scene_tested.renderer.get_frame())

    return real_test


def _control_data_path(module_name: str, test_name: str, setting_test: bool) -> Path:
    path = PATH_CONTROL_DATA / module_name
    if setting_test:
        # Create the directory if not existing.
        path.mkdir(exist_ok=True)
    if not setting_test and not path.exists():
        raise Exception(f"The control frames directory can't be found  in {path}")
    path = (path / test_name).with_suffix(".npz")
    if not setting_test and not path.is_file():
        raise Exception(
            f"The control frame for the test {test_name} cannot be found in {path.parent}. "
            "Make sure you generated the control frames first."
        )
    return path


def _config_test(last_frame: bool) -> ManimConfig:
    return ManimConfig().digest_file(
        str(
            Path(__file__).parent
            / (
                "config_graphical_tests_monoframe.cfg"
                if last_frame
                else "config_graphical_tests_multiframes.cfg"
            )
        )
    )
