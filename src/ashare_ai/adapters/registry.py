from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any, cast

from ashare_ai.adapters.protocols import RawDataAdapter

AdapterFactory = Callable[..., RawDataAdapter]


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, name: str, factory: AdapterFactory, *, replace: bool = False) -> None:
        key = name.casefold()
        if not replace and key in self._factories:
            raise ValueError(f"adapter already registered: {name}")
        self._factories[key] = factory

    def register_lazy(
        self, name: str, module_name: str, attribute: str, *, replace: bool = False
    ) -> None:
        def factory(**kwargs: Any) -> RawDataAdapter:
            module = import_module(module_name)
            adapter_type = getattr(module, attribute)
            return cast(RawDataAdapter, adapter_type(**kwargs))

        self.register(name, factory, replace=replace)

    def create(self, name: str, **kwargs: Any) -> RawDataAdapter:
        try:
            factory = self._factories[name.casefold()]
        except KeyError as exc:
            raise KeyError(f"unknown adapter: {name}; registered={self.names()}") from exc
        return factory(**kwargs)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


def default_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register_lazy("akshare", "ashare_ai.adapters.akshare", "AKShareAdapter")
    registry.register_lazy("tushare", "ashare_ai.adapters.tushare", "TushareAdapter")
    registry.register_lazy("eastmoney", "ashare_ai.adapters.eastmoney", "EastmoneyAdapter")
    return registry
