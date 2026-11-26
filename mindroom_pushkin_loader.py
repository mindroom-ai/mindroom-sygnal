# -*- coding: utf-8 -*-
# Copyright 2026 MindRoom contributors
#
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Resilient Sygnal pushkin loaders with fallback to stock APNs/GCM pushkins."""

import importlib
import logging
from typing import TYPE_CHECKING, Any, Dict, Type, TypeVar

from sygnal.apnspushkin import ApnsPushkin
from sygnal.gcmpushkin import GcmPushkin

if TYPE_CHECKING:
    from sygnal.sygnal import Sygnal

logger = logging.getLogger(__name__)
_PushkinT = TypeVar("_PushkinT", ApnsPushkin, GcmPushkin)


async def _load_pushkin(
    attr: str, base_cls: Type[_PushkinT], name: str, sygnal: "Sygnal", config: Dict[str, Any]
) -> _PushkinT:
    try:
        mod = importlib.import_module("mindroom_pushkin")
        pushkin = await getattr(mod, attr).create(name, sygnal, config)
        if not isinstance(pushkin, base_cls):
            raise TypeError(f"Expected {base_cls.__name__}, got {type(pushkin)!r}")
        return pushkin
    except Exception:
        logger.exception(
            "Failed to initialize %s for %s; falling back to stock %s",
            attr,
            name,
            base_cls.__name__,
        )
        return await base_cls.create(name, sygnal, config)


class SafeApnsPushkin:
    """Factory for MindroomApnsPushkin that falls back to stock ApnsPushkin."""

    @classmethod
    async def create(
        cls, name: str, sygnal: "Sygnal", config: Dict[str, Any]
    ) -> ApnsPushkin:
        return await _load_pushkin("MindroomApnsPushkin", ApnsPushkin, name, sygnal, config)


class SafeFcmPushkin:
    """Factory for MindroomFcmPushkin that falls back to stock GcmPushkin."""

    @classmethod
    async def create(
        cls, name: str, sygnal: "Sygnal", config: Dict[str, Any]
    ) -> GcmPushkin:
        return await _load_pushkin("MindroomFcmPushkin", GcmPushkin, name, sygnal, config)
