import dataclasses as dc
import typing as t

from superduper import CFG, logging
from superduper.backends.base.query import Query
from superduper.base.annotations import trigger
from superduper.base.datalayer import Datalayer
from superduper.components.cdc import CDC
from superduper.components.model import Mapping
from superduper.components.table import Table
from superduper.misc import typing as st

from .model import Model

if t.TYPE_CHECKING:
    from superduper.base.datalayer import Datalayer


SELECT_TEMPLATE = {'documents': [], 'query': '<collection_name>.find()'}


class Listener(CDC):
    """Listener component.

    Listener object which is used to process a column/key of a collection or table,
    and store the outputs.

    :param key: Key to be bound to the model.
    :param model: Model for processing data.
    :param predict_kwargs: Keyword arguments to self.model.predict().
    :param select: Query to "listen" for input on.
    :param identifier: A string used to identify the listener and it's outputs.
    :param output_table: Table to store the outputs.
    :param flatten: Flatten the output into separate records if ``True``.
    """

    breaks: t.ClassVar[t.Sequence[str]] = ('model', 'key', 'select')

    key: st.JSON
    model: Model
    predict_kwargs: t.Optional[t.Dict] = dc.field(default_factory=dict)
    select: t.Optional[Query] = None
    cdc_table: str = ''
    output_table: t.Optional[Table] = None
    flatten: bool = False

    def postinit(self):
        """Post initialization method."""
        if not self.cdc_table and self.select:
            self.cdc_table = self.select.table
        self._set_upstream()
        if isinstance(self.key, tuple):
            self.key = list(self.key)

        self.output_table = Table(
            self.outputs, fields={self.outputs: self.model.datatype, '_source': 'str'}
        )
        super().postinit()

    def handle_update_or_same(self, other):
        """If the component is new, but does not contain breaking changes.

        :param other: Other listener object.
        """
        super().handle_update_or_same(other)
        other.output_table = self.output_table

    def _set_upstream(self):
        deps = self.dependencies
        if deps:
            if not self.upstream:
                self.upstream = []
            try:
                it = 0
                for dep in deps:
                    identifier, uuid = dep
                    self.upstream.append(f'&:component:Listener:{identifier}:{uuid}')
                    it += 1
            except ValueError as e:
                if 'not enough values' in str(e):
                    logging.warn(
                        'Deferring dependencies to pre_create based on '
                        f'dependency {deps[it]}'
                    )

        if not self.upstream:
            return

        from collections import defaultdict

        from superduper import Component

        # This is to perform deduplication, in case an upstream
        # listener has already been provided

        huuids = [x.huuid if isinstance(x, Component) else x for x in self.upstream]
        huuids = defaultdict(lambda: [])
        for x in self.upstream:
            if isinstance(x, Component):
                huuids['&:' + x.huuid].append(x)
            else:
                huuids[x].append(x)

        self.upstream = [x[0] for x in huuids.values()]

    @property
    def predict_id(self):
        """Predict ID property."""
        return f'{self.identifier}__{self.uuid}'

    @staticmethod
    def _complete_key(key, db, listener_uuids=()):
        if isinstance(key, str) and key.startswith(CFG.output_prefix):
            if len(key[len(CFG.output_prefix) :].split('__')) == 2:
                return key
            identifier_and_sub_key = key[len(CFG.output_prefix) :].split('.', 1)
            if len(identifier_and_sub_key) == 2:
                identifier, sub_key = identifier_and_sub_key
            else:
                identifier = identifier_and_sub_key[0]
                sub_key = ''

            key = CFG.output_prefix + identifier
            try:
                uuid = listener_uuids[identifier]
            except KeyError:
                try:
                    uuid = db.show('listener', identifier, -1)['uuid']
                except FileNotFoundError:
                    raise Exception(
                        'Couldn\'t complete `Listener` key '
                        f'based on ellipsis {key}__????????????????. '
                        'Please specify using upstream_listener.outputs'
                    )

            complete_key = key + '__' + uuid
            if sub_key:
                complete_key += '.' + sub_key
            return complete_key
        elif isinstance(key, str):
            return key
        elif isinstance(key, list):
            return [Listener._complete_key(k, db, listener_uuids) for k in key]
        elif isinstance(key, tuple):
            return tuple([Listener._complete_key(k, db, listener_uuids) for k in key])
        elif isinstance(key, dict):
            return {
                Listener._complete_key(k, db, listener_uuids): v for k, v in key.items()
            }
        raise Exception(f'Invalid key type: {type(key)}')

    def _auto_fill_data(self, db: Datalayer):
        listener_keys = [k for k in db.startup_cache if k.startswith(CFG.output_prefix)]
        listener_predict_ids = [k[len(CFG.output_prefix) :] for k in listener_keys]
        lookup: t.Dict = dict(tuple(x.split('__')) for x in listener_predict_ids)
        assert self.select is not None
        self.select = self.select.complete_uuids(db, listener_uuids=lookup)
        if CFG.output_prefix in str(self.key):
            self.key = self._complete_key(self.key, db, listener_uuids=lookup)

        if self.cdc_table.startswith(CFG.output_prefix):
            self.cdc_table = self.select.table

    def _pre_create(self, db: Datalayer, startup_cache: t.Dict = {}):
        """Pre-create hook."""
        if self.select is None:
            return

        # TODO deprecate this
        self._auto_fill_data(db)

    @property
    def mapping(self):
        """Mapping property."""
        return Mapping(self.key, signature=self.model.signature)

    @property
    def outputs(self):
        """Get reference to outputs of listener model."""
        return f'{CFG.output_prefix}{self.predict_id}'

    @property
    def dependencies(self):
        """Listener model dependencies."""
        # args, kwargs = self.mapping.mapping
        key = self.key[:]
        if isinstance(key, str):
            all_ = [key]
        elif isinstance(key, (list, tuple)) and isinstance(key[0], str):
            all_ = list(key)
        else:
            all_ = list(key[0]) + list(key[1].values())

        # all_ = list(args) + list(kwargs.values())
        out = []
        for x in all_:
            if x.startswith(CFG.output_prefix):
                out.append(tuple(x[len(CFG.output_prefix) :].split('.')[0].split('__')))
        return out

    @trigger('apply', 'insert', 'update', requires='select')
    def run(self, ids: t.Sequence[str] | None = None) -> t.List[str]:
        """Run the listener."""
        assert self.select is not None
        # Returns a list of ids where the outputs were inserted
        out = self.model.predict_in_db(
            X=self.key,
            predict_id=self.predict_id,
            select=self.select,
            ids=ids,
            flatten=self.flatten,
            **(self.predict_kwargs or {}),
        )
        return out

    def cleanup(self, db: "Datalayer") -> None:
        """Clean up when the listener is deleted.

        :param db: Data layer instance to process.
        """
        super().cleanup(db=db)
        if self.select is not None:
            db.databackend.drop_table(self.outputs)
