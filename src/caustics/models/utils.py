# mypy: disable-error-code="union-attr, valid-type, has-type, assignment, arg-type, dict-item, return-value, misc"
from typing import List, Literal, Dict, Annotated, Union, Optional, Any, Tuple
import inspect
from pydantic import Field, create_model, field_validator
import torch

from ..parametrized import Parametrized
from .base_models import Base, Parameters, InitKwargs
from .registry import get_kind, _registry
from ..parametrized import ClassParam

PARAMS = "params"
INIT_KWARGS = "init_kwargs"


def _get_kwargs_field_definitions(
    parametrized_class: Parametrized, dependant_models: Dict[str, Any] = {}
) -> Dict[str, Dict[str, Any]]:
    """
    Get the field definitions for the parameters and init_kwargs of a Parametrized class

    Parameters
    ----------
    parametrized_class : Parametrized
        The Parametrized class to get the field definitions for.
    dependant_models : Dict[str, Any], optional
        The dependent models to use, by default {}
        See: https://docs.pydantic.dev/latest/concepts/unions/#nested-discriminated-unions

    Returns
    -------
    dict
        The resulting field definitions dictionary
    """
    cls_signature = inspect.signature(parametrized_class)
    kwargs_field_definitions: Dict[str, Dict[str, Any]] = {PARAMS: {}, INIT_KWARGS: {}}
    for k, v in cls_signature.parameters.items():
        if k != "name":
            anno = v.annotation
            dtype = anno.__origin__
            cls_param = ClassParam(*anno.__metadata__)
            if cls_param.isParam:
                kwargs_field_definitions[PARAMS][k] = (
                    dtype,
                    Field(default=v.default, description=cls_param.description),
                )
            # Below is to handle cases for init kwargs
            elif k in dependant_models:
                dependant_model = dependant_models[k]
                if isinstance(dependant_model, list):
                    # For the multi lens case
                    # dependent model is wrapped in a list
                    dependant_model = dependant_model[0]
                    kwargs_field_definitions[INIT_KWARGS][k] = (
                        List[dependant_model],
                        Field([], description=cls_param.description),
                    )
                else:
                    kwargs_field_definitions[INIT_KWARGS][k] = (
                        dependant_model,
                        Field(..., description=cls_param.description),
                    )
            elif v.default == inspect._empty:
                kwargs_field_definitions[INIT_KWARGS][k] = (
                    dtype,
                    Field(..., description=cls_param.description),
                )
            else:
                kwargs_field_definitions[INIT_KWARGS][k] = (
                    dtype,
                    Field(v.default, description=cls_param.description),
                )
    return kwargs_field_definitions


def create_pydantic_model(
    cls: "Parametrized | str", dependant_models: Dict[str, type] = {}
) -> Base:
    """
    Create a pydantic model from a Parametrized class.

    Parameters
    ----------
    cls : Parametrized | str
        The Parametrized class to create the model from.
    dependant_models : Dict[str, type], optional
        The dependent models to use, by default {}
        See: https://docs.pydantic.dev/latest/concepts/unions/#nested-discriminated-unions

    Returns
    -------
    Base
        The pydantic model of the Parametrized class.
    """
    if isinstance(cls, str):
        parametrized_class = get_kind(cls)  # type: ignore

    # Get the field definitions for parameters and init_kwargs
    kwargs_field_definitions = _get_kwargs_field_definitions(
        parametrized_class, dependant_models
    )

    # Setup the pydantic models for the parameters and init_kwargs
    ParamsModel = create_model(
        f"{parametrized_class.__name__}_Params",
        __base__=Parameters,
        __validators__={
            # Convert to tensor before passing to the model for additional validation
            "tensors_validator": field_validator("*", mode="before")(
                lambda cls, v: (  # noqa
                    torch.as_tensor(v) if not isinstance(v, torch.Tensor) else v
                )
            )
        },
        **kwargs_field_definitions[PARAMS],
    )
    InitKwargsModel = create_model(
        f"{parametrized_class.__name__}_Init_Kwargs",
        __base__=InitKwargs,
        **kwargs_field_definitions[INIT_KWARGS],
    )

    # Setup the field definitions for the model
    field_definitions = {
        "kind": (Literal[parametrized_class.__name__], Field(parametrized_class.__name__)),  # type: ignore
        "params": (
            ParamsModel,
            Field(ParamsModel(), description="Parameters of the object"),
        ),
        "init_kwargs": (
            Optional[InitKwargsModel],
            Field(None, description="Initiation keyword arguments of the object"),
        ),
    }

    # Create the model
    model = create_model(
        parametrized_class.__name__, __base__=Base, **field_definitions
    )
    # Set the imported parametrized class to the model
    # this will be accessible as `model._cls`
    model = model._set_class(parametrized_class)
    return model


def setup_pydantic_models() -> Tuple[type[Annotated], type[Annotated]]:
    """
    Setup the pydantic models for the light sources and lenses.

    Returns
    -------
    light_sources : type[Annotated]
        The annotated union of the light source pydantic models
    lenses : type[Annotated]
        The annotated union of the lens pydantic models
    """
    # Cosmology
    cosmology_models = [create_pydantic_model(cosmo) for cosmo in _registry.cosmology]
    cosmology = Annotated[Union[tuple(cosmology_models)], Field(discriminator="kind")]
    # Light
    light_models = [create_pydantic_model(light) for light in _registry.light]
    light_sources = Annotated[Union[tuple(light_models)], Field(discriminator="kind")]
    # Single Lens
    lens_dependant_models = {"cosmology": cosmology}
    single_lens_models = [
        create_pydantic_model(lens, dependant_models=lens_dependant_models)
        for lens in _registry.single_lenses
    ]
    single_lenses = Annotated[
        Union[tuple(single_lens_models)], Field(discriminator="kind")
    ]
    # Multi Lens
    multi_lens_models = [
        create_pydantic_model(
            lens, dependant_models={"lenses": [single_lenses], **lens_dependant_models}
        )
        for lens in _registry.multi_lenses
    ]
    lenses = Annotated[
        Union[tuple([*single_lens_models, *multi_lens_models])],
        Field(discriminator="kind"),
    ]
    return light_sources, lenses


def setup_simulator_models() -> type[Annotated]:
    """
    Setup the pydantic models for the simulators

    Returns
    -------
    type[Annotated]
        The annotated union of the simulator pydantic models
    """
    light_sources, lenses = setup_pydantic_models()
    # Hard code the dependants for now
    # there's currently only one simulator
    # in the system.
    dependents = {
        "Lens_Source": {
            "source": light_sources,
            "lens_light": light_sources,
            "lens": lenses,
        }
    }
    simulators_models = [
        create_pydantic_model(sim, dependant_models=dependents.get(sim))
        for sim in _registry.simulators
    ]
    return Annotated[Union[tuple(simulators_models)], Field(discriminator="kind")]