from functools import update_wrapper
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Tuple, TypeVar, cast

from .main import BaseConfig, Extra, create_model
from .utils import to_camel

__all__ = 'validate_arguments', 'DecoratorSetupError'

if TYPE_CHECKING:
    from .main import BaseModel
    from .typing import AnyCallable

    Callable = TypeVar('Callable', bound=AnyCallable)


def validate_arguments(function: 'Callable') -> 'Callable':
    """
    Decorator to validate the arguments to a function.
    """
    vd = ValidatedFunction(function)
    vd = update_wrapper(vd, function)  # type: ignore
    return cast('Callable', vd)


class DecoratorSetupError(TypeError):
    pass


class DecoratorModelConfig(BaseConfig):
    extra = Extra.forbid


ALT_VAR_ARGS = 'var__args'
ALT_VAR_KWARGS = 'var__kwargs'


class ValidatedFunction:
    def __init__(self, function: 'Callable'):
        from inspect import signature, Parameter

        self.raw_function = function
        self.arg_mapping: Dict[int, str] = {}
        self.args_field_name = 'args'
        self.kwargs_field_name = 'kwargs'
        self.positional_only_args = set()

        parameters: Mapping[str, Parameter] = signature(function).parameters

        if parameters.keys() & {ALT_VAR_ARGS, ALT_VAR_KWARGS}:
            raise DecoratorSetupError(
                f'"{ALT_VAR_ARGS}" and "{ALT_VAR_KWARGS}" are not permitted as argument names when '
                f'using the "validate_arguments" decorator'
            )

        allows_var_args = False
        allows_var_kwargs = False
        fields: Dict[str, Any] = {}
        for i, (name, p) in enumerate(parameters.items()):
            if p.annotation == p.empty:
                annotation = Any
            else:
                # TODO if str update forward ref
                annotation = p.annotation

            default = ... if p.default == p.empty else p.default
            if p.kind == Parameter.POSITIONAL_ONLY:
                self.arg_mapping[i] = name
                fields[name] = annotation, default
                self.positional_only_args.add(name)
            elif p.kind == Parameter.POSITIONAL_OR_KEYWORD:
                self.arg_mapping[i] = name
                fields[name] = annotation, default
            elif p.kind == Parameter.KEYWORD_ONLY:
                fields[name] = annotation, default
            elif p.kind == Parameter.VAR_POSITIONAL:
                self._args_field_name = name
                allows_var_args = True
                fields[name] = Tuple[annotation, ...], None
            else:
                assert p.kind == Parameter.VAR_KEYWORD, p.kind
                self._kwargs_field_name = name
                allows_var_kwargs = True
                fields[name] = Dict[str, annotation], None  # type: ignore

        # these checks avoid a clash between "args" and a field with that name
        if not allows_var_args and self.args_field_name in fields:
            self.args_field_name = ALT_VAR_ARGS

        # same with "kwargs"
        if not allows_var_kwargs and self.kwargs_field_name in fields:
            self.kwargs_field_name = ALT_VAR_KWARGS

        self.model = create_model(to_camel(function.__name__), __config__=DecoratorModelConfig, **fields)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        values, has_var_args, has_var_kwargs = self.build_values(args, kwargs)
        m = self.model(**values)
        return self.execute(m, has_var_args, has_var_kwargs)

    def build_values(self, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, bool]:
        values: Dict[str, Any] = {}
        has_var_args = False
        has_var_kwargs = False
        if args:
            arg_iter = enumerate(args)
            while True:
                try:
                    i, a = next(arg_iter)
                except StopIteration:
                    break
                arg_name = self.arg_mapping.get(i)
                if arg_name is not None:
                    values[arg_name] = a
                else:
                    values[self.args_field_name] = [a] + [a for _, a in arg_iter]
                    has_var_args = True
                    break

        var_kwargs = {}
        for k, v in kwargs.items():
            if k in self.model.__fields__:
                if k in self.positional_only_args:
                    raise NotImplementedError('TODO deal with position only errors better')
                values[k] = v
            else:
                var_kwargs[k] = v
        if var_kwargs:
            values[self.kwargs_field_name] = var_kwargs
            has_var_kwargs = True
        return values, has_var_args, has_var_kwargs

    def execute(self, m: 'BaseModel', has_var_args: bool, has_var_kwargs: bool) -> Any:
        d = {k: v for k, v in m._iter() if k in m.__fields_set__}
        if has_var_kwargs:
            d.update(d.pop(self.kwargs_field_name))

        if has_var_args:
            args_: List[Any] = []
            in_kwargs = False
            kwargs = {}
            for name, value in d.items():
                if in_kwargs:
                    kwargs[name] = value
                elif name == self.args_field_name:
                    args_ += value
                    in_kwargs = True
                else:
                    args_.append(value)
            return self.raw_function(*args_, **kwargs)
        elif self.positional_only_args:
            args_ = []
            kwargs = {}
            for name, value in d.items():
                if name in self.positional_only_args:
                    args_.append(value)
                else:
                    kwargs[name] = value
            return self.raw_function(*args_, **kwargs)
        else:
            return self.raw_function(**d)