"""Parse FGD files, used to describe Hammer entities."""
from enum import Enum
import re

from typing import List, Tuple, Dict, Iterator, Union

from srctools import Vec
from srctools.filesys import FileSystem, File
from srctools.tokenizer import Tokenizer, Token, TokenSyntaxError

__all__ = [
    'ValueTypes', 'EntityTypes'
    'KeyValError', 'FGD', 'EntityDef',
]

# "text" +
_RE_DOC_LINE = re.compile(r'\s*"([^"]*)"\s*(\+)?\s*')

_RE_KEYVAL_LINE = re.compile(
    r''' (input | output)? \s* # Input or output name
    (\w+)\s*\(\s*(\w+)\s*\) # Name, (type)
    \s* (report | readonly)?  # Flags for the text
    (?: \s* : \s* \"([^"]*)\"\s* # Display name
        (\+)? \s* # IO only - plus for continued description
        (?::([^:]+)  # Default
            (?::([^:]+)  # Docs
            )?
        )?
    )? # Optional for spawnflags..
    \s* (=)? # Has equal sign?
    ''',
    re.VERBOSE,
)

_RE_HELPERS = re.compile(
    r'''(\w+)\s* \( \s* ([^)]*) \s* \)''',
    re.VERBOSE,
)
_RE_HELPER_ARGS = re.compile(r'\s*\,\s*')

class FGDParseError(TokenSyntaxError):
    pass

class ValueTypes(Enum):
    """Types which can be applied to a KeyValue."""
    # Special cases:
    VOID = 'void'  # Nothing
    CHOICES = 'choices'  # Special - preset value list as string
    SPAWNFLAGS = 'flags'  # Binary flag values.

    # Simple values
    STRING = 'string'
    BOOL = 'boolean'
    INT = 'integer'
    FLOAT = 'float'
    VEC = 'vector'  # Offset or the like
    ANGLES = 'angle'  # Rotation

    # String targetname values (need fixups)
    TARG_DEST = 'target_destination'  # A targetname of another ent.
    TARG_DEST_CLASS = 'target_name_or_class'  # Above + classnames.
    TARG_SOURCE = 'target_source'  # The 'targetname' keyvalue.
    TARG_NPC_CLASS = 'npcclass'  # targetnames filtered to NPC ents
    TARG_POINT_CLASS = 'pointentityclass'  # targetnames filtered to point entities.
    TARG_FILTER_NAME = 'filterclass'  # targetnames of filters.
    TARG_NODE_DEST = 'node_dest'  # name of a node
    TARG_NODE_SOURCE = 'node_id'  # name of us

    # Strings, don't need fixups
    STR_SCENE = 'scene'  # VCD files
    STR_SOUND = 'sound'  # WAV & SoundScript
    STR_PARTICLE = 'particlesystem'  # Particles
    STR_SPRITE = 'sprite'  # Sprite materials
    STR_DECAL = 'decal'  # Sprite materials
    STR_MATERIAL = 'material'  # Materials
    STR_MODEL = 'studio'  # Model
    STR_VSCIPT = 'scriptlist'  # List of vscripts

    # More complex
    ANGLE_NEG_PITCH = 'angle_negative_pitch'  # Inverse pitch of 'angles'
    VEC_LINE = 'vecline'  # Absolute vector, with line drawn from origin to point
    VEC_ORIGIN = 'origin'  # Used for 'origin' keyvalue
    VEC_AXIS = 'axis'
    COLOR_1 = 'color1'  # RGB 0-1 + extra
    COLOR_255 = 'color255'  # RGB 0-255 + extra
    SIDE_LIST = 'sidelist'  # Space-seperated list of sides.

    # Instances
    INST_FILE = 'instance_file'  # File of func_instance
    INST_VAR_DEF = 'instance_parm'  # $fixup definition
    INST_VAR_REP = 'instance_variable'  # $fixup usage

    @property
    def has_list(self):
        """Is this a flag or choices value, and needs a [] list?"""
        return self.value in ('choices', 'flags')

VALUE_TYPE_LOOKUP = {
    typ.value: typ
    for typ in ValueTypes
}
# These have two names pointing to the same type...
VALUE_TYPE_LOOKUP['bool'] = ValueTypes.BOOL
VALUE_TYPE_LOOKUP['int'] = ValueTypes.INT


class EntityTypes(Enum):
    BASE = 'baseclass'  # Not an entity, others inherit from this.
    POINT = 'pointclass'  # Point entity
    BRUSH = 'solidclass'  # Brush entity. Can't have 'model'
    ROPES = 'keyframeclass'  # Used for move_rope etc
    TRACK = 'moveclass'  # Used for path_track etc
    FILTER = 'filterclass'  # Used for filters
    NPC = 'npcclass'  # An NPC


class HelperTypes(Enum):
    """Types of functions in the entity header."""
    INHERIT = 'base'

    # Snap to 1/2 of grid.
    # Special - no arguments.
    HALF_GRID_SNAP = 'halfgridsnap'

    # Simple helpers
    CUBE = 'size'  # Sets size of purple cube
    BBOX = 'bbox'  # Sets bounding box of entity
    TINT = 'color'
    SPHERE = 'sphere'
    LINE = 'line'
    FRUSTUM = 'frustum'
    CYLINDER = 'cylinder'
    BRUSH_SIDES = 'sidelist'
    BOUNDING_BOX_HELPER = 'wirebox'  # Displays bounding box from two keyvalues

    # Complex helpers using resources
    SPRITE = 'iconsprite'
    MODEL = 'studio'
    MODEL_PROP = 'studioprop'
    MODEL_NEG_PITCH = 'lightprop'  # Uses separate pitch keyvalue

    # Specialty for certain ents
    ENT_SPRITE = 'sprite'
    ENT_INSTANCE = 'instance'
    ENT_DECAL = 'decal'
    ENT_OVERLAY = 'overlay'
    ENT_OVERLAY_WATER = 'overlay_transition'
    ENT_LIGHT = 'light'
    ENT_LIGHT_CONE = 'lightcone'
    ENT_ROPE = 'keyframe'
    ENT_TRACK = 'animator'
    ENT_BREAKABLE_SURF = 'quadbounds'  # Sets the 4 corners on save


def read_colon_list(tok: Tokenizer, had_colon=False):
    """Read strings seperated by colons, up to the end of the line.
    
    The token found at the end is returned.
    """
    strings = []
    ready_for_string = had_colon  # Did we have a colon before?
    token = Token.EOF
    for token, tok_value in tok:
        if token is Token.STRING:
            if not ready_for_string:
                raise tok.error('Too many strings ({!r})!', tok_value)
            strings.append(tok_value)
            ready_for_string = False
        elif token is Token.COLON:
            if ready_for_string:
                # ': :' means to have an empty string there.
                strings.append('')
            ready_for_string = True
        elif token is Token.PLUS:
            if ready_for_string or not strings:
                raise tok.error('"+" without a string before it!')
            strings[-1] += tok.expect(Token.STRING)
        elif ready_for_string and token is Token.NEWLINE:
            continue # skip over this in particular..
        else:
            if ready_for_string:
                raise tok.error(token)
            return strings, token
    else:
        raise tok.error(token)


class KeyValues:
    """Represents a generic keyvalue type."""
    def __init__(self, name, val_type, disp_name, default, doc, val_list, is_readonly):
        self.name = name
        self.type = val_type
        self.default = default
        self.disp_name = disp_name
        self.desc = doc
        self.val_list = val_list
        self.readonly = is_readonly

class IODef:
    """Represents an input or output for an entity."""
    def __init__(self, name, val_type: ValueTypes, description: str):
        self.name = name
        self.type = val_type
        self.desc = description


class EntityDef:
    """A definition for an entity."""
    def __init__(self, type: EntityTypes):
        self.type = type
        self.classname = ''
        self.keyvalues = {}
        self.inputs = {}
        self.outputs = {}
        # Base type names - base()
        self.bases = []
        # line(), studio(), etc in the header
        # this is a func, args tuple.
        self.helpers = []
        self.desc = []

    @classmethod
    def parse(
        cls,
        fgd: 'FGD',
        tok: Tokenizer,
        ent_type: EntityTypes,
    ):
        """Parse an entity definition."""
        entity = cls(ent_type)

        # First parse the bases part - lots of name(args) sections until an '='
        help_type = None
        for token, token_value in tok:
            if token is Token.NEWLINE:
                continue
            if token is Token.STRING:
                if help_type is None:
                    try:
                        help_type = HelperTypes(token_value)
                    except ValueError:
                        raise tok.error(
                            'Unknown HelperType "{}"!',
                            token_value,
                        )
                    continue
                else:
                    # No arguments for the previous helper - add it in like that.
                    entity.helpers.append((help_type, ''))

            elif token is Token.PAREN_ARGS:
                if help_type is None:
                    raise tok.error('Args without helper type! ({!r})', token_value)

                args = _RE_HELPER_ARGS.split(token_value)

                if help_type is HelperTypes.INHERIT:
                    for base in args:
                        base = base.casefold()
                        if base not in entity.bases:
                            entity.bases.append(base.strip())
                    help_type = None
                    continue

                entity.helpers.append((help_type, args))

                help_type = None

            elif token is Token.EQUALS:
                break
            else:
                raise tok.error(token)
        else:
            raise tok.error('Entity header never ended!')

        # We were waiting for arguments for the previous helper.
        # We need to add with none.
        if help_type:
            entity.helpers.append((help_type, ''))

        entity.classname = tok.expect(Token.STRING).strip()

        # We next might have a ':' then docstring before the [,
        # or directly to [.
        desc = None
        for doc_token, token_value in tok:
            if doc_token is Token.NEWLINE:
                continue
            if doc_token is Token.COLON:
                if desc is None:
                    desc = []
                else:
                    raise tok.error('Two colons in entity description!')
            elif doc_token is Token.STRING:
                if desc is None or desc:
                    # No colon yet, or we have text without '+' between
                    raise tok.error(doc_token)
                desc.append(token_value)
            elif doc_token is Token.PLUS:
                if not desc:
                    raise tok.error('+ without string before it!')
                desc.append(tok.expect(Token.STRING))
            elif doc_token is Token.BRACK_OPEN:
                if desc:
                    entity.desc = ''.join(desc)
                break
            else:
                raise tok.error(doc_token)

        fgd.entities[entity.classname.casefold()] = entity

        # Now parse keyvalues, and input/outputs
        for token, token_value in tok:
            if token is Token.BRACK_CLOSE:
                break  # End of this entity.

            if token is Token.NEWLINE:
                continue

            # IO - keyword at the start.
            if token is not Token.STRING:
                raise tok.error(token)

            io_type = token_value.casefold()
            if io_type in ('input', 'output'):

                name = tok.expect(Token.STRING)
                raw_value_type = tok.expect(Token.PAREN_ARGS).strip()
                try:
                    val_typ = VALUE_TYPE_LOOKUP[raw_value_type.casefold()]
                except KeyError:
                    raise tok.error('Unknown keyvalue type "{}"!', raw_value_type)

                # Can't have a spawnflags or choices input type...
                if val_typ.has_list:
                    raise tok.error(
                        '"{}" value type is not valid for an input or output!',
                        val_typ.value,
                    )

                # Read desc
                attrs, token = read_colon_list(tok)

                if token is token.EQUALS:
                    raise tok.error(token)

                if attrs:
                    try:
                        [desc] = attrs
                    except ValueError:
                        raise tok.error('Too many values for IO definition!')
                else:
                    desc = ''

                # entity.inputs or entity.outputs
                getattr(entity, io_type + 's')[name] = IODef(name, val_typ, desc)

            else:
                # Keyvalue
                name = io_type

                raw_value_type = tok.expect(Token.PAREN_ARGS).strip()
                try:
                    val_typ = VALUE_TYPE_LOOKUP[raw_value_type.casefold()]
                except KeyError:
                    raise tok.error('Unknown keyvalue type "{}"!', raw_value_type)

                next_token, key_flag = tok()

                is_readonly = False
                had_colon = False
                attrs = None

                if next_token is Token.STRING:
                    # 'report' or 'readonly'
                    if key_flag.casefold() == 'readonly':
                        is_readonly = True
                elif next_token is Token.COLON:
                    had_colon = True
                elif next_token is Token.EQUALS:
                    # Special case - spawnflags doesn't have to have
                    # any info - skips straight to the end.
                    if val_typ is ValueTypes.SPAWNFLAGS:
                        attrs = []
                        has_equal = next_token
                elif next_token is Token.NEWLINE:
                    attrs = []
                    has_equal = next_token
                else:
                    raise tok.error(next_token)

                if attrs is None:
                    attrs, has_equal = read_colon_list(tok, had_colon)
                attr_len = len(attrs)

                desc = ''
                default = None
                if attr_len == 3:
                    disp_name, default, desc = attrs
                elif attr_len == 2:
                    disp_name, default = attrs
                elif attr_len == 1:
                    [disp_name] = attrs
                elif attr_len == 0:
                    disp_name = name
                else:
                    raise tok.error('Too many attributes for keyvalue!\n{!r}', attrs)

                if val_typ.has_list:
                    if has_equal is not Token.EQUALS:
                        raise tok.error('No list for "{}" value type!', val_typ.name)
                    # Read the choices in the []
                    val_list = []
                    tok.expect(Token.BRACK_OPEN)
                    for choices_token, choices_value in tok:
                        if choices_token is Token.NEWLINE:
                            continue
                        if choices_token is Token.BRACK_CLOSE:
                            break
                        elif choices_token is not Token.STRING:
                            raise tok.error(choices_token)
                        vals, has_equal = read_colon_list(tok, had_colon=False)

                        # Spawnflags can have a default, others don't
                        if len(vals) == 2 and val_typ is ValueTypes.SPAWNFLAGS:
                            val_list.append((choices_value, vals[0], bool(vals[1])))
                        elif len(vals) == 1:
                            if val_typ is ValueTypes.SPAWNFLAGS:
                                val_list.append((choices_value, vals[0], True))
                            else:
                                val_list.append((choices_value, vals[0]))
                        elif len(vals) == 0:
                            raise tok.error(Token.STRING)
                        else:
                            raise tok.error('Too many values!\n{}', vals)

                        # Handle ] at the end of a : : line.
                        if has_equal is Token.BRACK_CLOSE:
                            break
                    else:
                        raise tok.error(token.EOF)
                else:
                    val_list = None
                    if has_equal is Token.EQUALS:
                        raise tok.error('"{}" value types can\'t have lists!', val_typ.name)

                entity.keyvalues[name.casefold()] = KeyValues(
                    name,
                    val_typ,
                    disp_name,
                    default,
                    desc,
                    val_list,
                    is_readonly == 'readonly',
                )

    def __repr__(self):
        if self.type is EntityTypes.BASE:
            return '<Entity Base "{}">'.format(self.classname)
        else:
            return '<Entity {}>'.format(self.classname)


class FGD:
    """A FGD set for a game. May be composed of several files."""
    def __init__(self):
        """Create a FGD."""
        # List of names we have already parsed.
        # We don't parse them again, to prevent infinite loops.
        self._parse_list = []
        # Entity definitions
        self.entities = {}  # type: Dict[str, EntityDef]
        # maximum bounding box of map
        self.map_size_min = 0
        self.map_size_max = 0

    @classmethod
    def parse(
        cls,
        file: Union[File, str],
        filesystem: FileSystem=None,
    ) -> 'FGD':
        """Parse an FGD file.

        Parameters:
        * file: A filesys.File representing the file to read, or a file path.
        * filesystem: The system to lookup files in. This is needed to 
          resolve file inclusions. If not passed, file must by a filesystem
          File to obtain a matching filesystem.
        """
        if filesystem is not None:
            if not file.endswith('.fgd'):
                file += '.fgd'
            try:
                with filesystem:
                    file = filesystem[file]
            except KeyError:
                raise FileNotFoundError(file)
        elif isinstance(file, File):
            filesystem = file.sys
        else:
            raise TypeError(
                'String file path passed ({!r}), but no filesystem!'.format(file)
            )
        fgd = cls()
        fgd._parse_file(filesystem, file)

        for ent in fgd:
            new_bases, orig_bases = ent.bases, orig_bases = [], ent.bases
            for base in orig_bases:
                try:
                    new_bases.append(fgd.entities[base])
                except KeyError:
                    raise ValueError(
                        'Unknown base ({}) for {}'.format(
                            orig_bases,
                            ent.classname,
                        )
                    )

        return fgd

    def _parse_file(self, filesys: FileSystem, file: File):
        """Parse one file (recursively if needed)."""

        if file in self._parse_list:
            return

        self._parse_list.append(file)

        with filesys, file.open_str() as f:
            tokeniser = Tokenizer(
                f,
                filename=file.path,
                error=FGDParseError,
                string_bracket=False,
            )
            for token, token_value in tokeniser:
                # The only things at top-level would be bare strings, and empty lines.
                if token is Token.NEWLINE:
                    continue
                if token is not Token.STRING:
                    raise tokeniser.error(token)
                token_value = token_value.casefold()

                if token_value == '@include':
                    include_file = tokeniser.expect(Token.STRING)
                    if not include_file.endswith('.fgd'):
                        include_file += '.fgd'

                    try:
                        include = filesys[include_file]
                    except KeyError:
                        raise FileNotFoundError(file)
                    self._parse_file(filesys, include)

                elif token_value == '@mapsize':
                    # Max/min map size definition
                    mapsize_args = tokeniser.expect(Token.PAREN_ARGS)
                    try:
                        min_size, max_size = mapsize_args.split(',')
                        self.map_size_min = int(min_size.strip())
                        self.map_size_max = int(max_size.strip())
                    except ValueError:
                        raise tokeniser.error(
                            'Invalid @MapSize: ({})',
                            mapsize_args,
                        )
                # Entity definition...
                elif token_value[:1] == '@':
                    try:
                        ent_type = EntityTypes(token_value[1:])
                    except ValueError:
                        raise tokeniser.error(
                            'Invalid Entity type "{}"!',
                            ent_type[1:],
                        )
                    EntityDef.parse(self, tokeniser, ent_type)
                else:
                    raise tokeniser.error('Bad keyword {!r}', token_value)

    def __getitem__(self, classname) -> EntityDef:
        try:
            return self.entities[classname.casefold()]
        except KeyError:
            raise KeyError('No class "{}"!'.format(classname)) from None

    def __iter__(self) -> Iterator[EntityDef]:
        return iter(self.entities.values())
