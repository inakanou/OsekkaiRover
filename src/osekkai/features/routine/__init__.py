"""日課の機能（daily-routine）。決まった時刻に、朝と夜のタスクを子どもに声かけする。

基盤はエントリーポイント（`pyproject.toml` の `osekkai.features`）から `RoutineFeature` を見つける。
"""

from osekkai.features.routine.feature import RoutineFeature

__all__ = ["RoutineFeature"]
