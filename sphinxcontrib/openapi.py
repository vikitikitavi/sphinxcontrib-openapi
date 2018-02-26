"""
    sphinxcontrib.openapi
    ---------------------

    The OpenAPI spec renderer for Sphinx. It's a new way to document your
    RESTful API. Based on ``sphinxcontrib-httpdomain``.

    :copyright: (c) 2016, Ihor Kalnytskyi.
    :license: BSD, see LICENSE for details.
"""

from __future__ import unicode_literals

import io
import itertools
import collections

import yaml
import jsonschema

from docutils import nodes
from docutils.parsers.rst import Directive, directives
from docutils.statemachine import ViewList

from sphinx.util.nodes import nested_parse_with_titles


# Dictionaries do not guarantee to preserve the keys order so when we load
# JSON or YAML - we may loose the order. In most cases it's not important
# because we're interested in data. However, in case of OpenAPI spec it'd
# be really nice to preserve them since, for example, endpoints may be
# grouped logically and that improved readability.
class _YamlOrderedLoader(yaml.SafeLoader):
    pass


_YamlOrderedLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    lambda loader, node: collections.OrderedDict(loader.construct_pairs(node))
)


def _resolve_refs(uri, spec):
    """Resolve JSON references in a given dictionary.

    OpenAPI spec may contain JSON references to its nodes or external
    sources, so any attempt to rely that there's some expected attribute
    in the spec may fail. So we need to resolve JSON references before
    we use it (i.e. replace with referenced object). For details see:

        https://tools.ietf.org/html/draft-pbryan-zyp-json-ref-02

    The input spec is modified in-place despite being returned from
    the function.
    """
    resolver = jsonschema.RefResolver(uri, spec)

    def _do_resolve(node):
        if isinstance(node, collections.Mapping) and '$ref' in node:
            with resolver.resolving(node['$ref']) as resolved:
                return resolved
        elif isinstance(node, collections.Mapping):
            for k, v in node.items():
                node[k] = _do_resolve(v)
        elif isinstance(node, (list, tuple)):
            for i in range(len(node)):
                node[i] = _do_resolve(node[i])
        return node

    return _do_resolve(spec)


def _collect_description(description):
    """Form the line from description"""
    result = ''
    for line in description.splitlines():
        result += '{line}'.format(**locals())
    return result


def _create_partition(partition_name):
    """Create bold partition line"""
    yield '**{name} :**'.format(name=partition_name)
    yield ''


def _print_parameters(parameters):
    """Print parameters list with it's type and description"""
    for param in parameters:
        req = param_is_required(param.get("required"))
        description = _collect_description(param.get('description', ''))
        yield '* {name} {req} (*{type}*) - {desc}'.format(
            **param,
            req=req,
            desc=description)
    yield ''


def param_is_required(required):
    if required:
        result = "``*``"
    else:
        result = ''
    return result


def _create_value_example(value):
    if isinstance(value, str):
        return "\"" + value + "\""
    return str(value)


def _create_object_schema_example(example, indent_number=1):
    """Print json example"""
    indent = '   '
    yield indent * indent_number + '{'

    for key, value in example.items():
        if isinstance(value, dict):
            yield indent * (indent_number + 1) + str(key) + ": "
            for line in iter(_create_object_schema_example(value, indent_number + 1)):
                yield line
        elif isinstance(value, list):
            yield indent * (indent_number + 1) + str(key) + ": "
            for line in iter(_create_list_schema_example(value, indent_number + 1)):
                yield line
        else:
            yield indent * (indent_number + 1) + str(key) + ": " + _create_value_example(value) + ','

    if indent_number == 1:
        yield indent * indent_number + '}'
    else:
        yield indent * indent_number + '},'


def _create_list_schema_example(example, indent_number=1):
    """Print list example"""
    indent = '   '
    yield indent * indent_number + '['

    for value in example:
        if isinstance(value, dict):
            for line in iter(_create_object_schema_example(value, indent_number + 1)):
                yield line
        elif isinstance(value, list):
            for line in iter(_create_list_schema_example(value, indent_number + 1)):
                yield line
        else:
            yield indent * (indent_number + 1) + _create_value_example(value) + ','

    if indent_number == 1:
        yield indent * indent_number + ']'
    else:
        yield indent * indent_number + '],'


def _create_schema_example(example, example_title="Example"):
    if not example:
        return None
    yield ''
    yield '{title} ::'.format(title=example_title)
    yield ''
    if isinstance(example, dict):
        for line in iter(_create_object_schema_example(example)):
            yield line
    elif isinstance(example, list):
        for line in iter(_create_list_schema_example(example)):
            yield line
    yield ''



def _httpresource(endpoint, method, properties):
    parameters = properties.get('parameters', [])
    responses = properties['responses']
    indent = '   '

    api = "{0} {1}".format(method, endpoint)
    api = api.replace('{', '{{')
    api = api.replace('}', '}}')
    yield api
    yield '*' * len(api)
    yield ''

    if 'summary' in properties:
        for line in properties['summary'].splitlines():
            yield '{line}'.format(**locals())

    yield _collect_description(properties['description'])
    yield ''

    # print request's route params
    path_parameters = list(filter(lambda p: p['in'] == 'path', parameters))
    if path_parameters:
        for line in iter(itertools.chain(_create_partition("Path parameters"))):
            yield line
        for line in iter(itertools.chain(_print_parameters(path_parameters))):
            yield line

    # print request's query params
    query_parameters = list(filter(lambda p: p['in'] == 'query', parameters))
    if query_parameters:
        for line in iter(itertools.chain(_create_partition("Query parameters"))):
            yield line
        for line in iter(itertools.chain(_print_parameters(query_parameters))):
            yield line

    # print request body params
    for param in filter(lambda p: p['in'] == 'body', parameters):
        for line in iter(itertools.chain(_create_partition("Body"))):
            yield line
        for _property, value in param.get("schema", {}).get("properties").items():
            description = _collect_description(param.get('description', ''))
            _range = ''
            if value.get("type") == 'integer':
                _range = "Range: (" + str(value.get('minimum', '-')) + ', ' + str(value.get('maximum', '-')) + ")."
            yield '* {name} (*{type}*) - {desc} {range}'.format(
                type=value.get("type"),
                name=_property,
                desc=description,
                range=_range)
        yield ''
        example = param.get("schema", {}).get("example", {})
        for line in iter(_create_schema_example(example)):
            yield line

    # print response status codes
    if responses.items():
        for line in iter(itertools.chain(_create_partition("Status code"))):
            yield line
        for status, response in responses.items():
            description = _collect_description(response.get('description', ''))
            yield '* {status} - {description}'.format(**locals())
            example = response.get("schema", {}).get("example", {})
            for line in iter(_create_schema_example(example, "Response example")):
                yield line
        yield ''

    # print request header params
    if list(filter(lambda p: p['in'] == 'header', parameters)):
        _create_partition("Request headers")
        for param in filter(lambda p: p['in'] == 'header', parameters):
            description = _collect_description(param.get('description', ''))
            yield '* {name} - {desc}'.format(**param, desc=description)
        yield ''

    # print response headers
    for status, response in responses.items():
        for headername, header in response.get('headers', {}).items():
            yield indent + ':resheader {name}:'.format(name=headername)
            for line in header['description'].splitlines():
                yield '{indent}{indent}{line}'.format(**locals())
    yield ''

    yield ''


def _normalize_spec(spec, **options):
    # OpenAPI spec may contain JSON references, so we need resolve them
    # before we access the actual values trying to build an httpdomain
    # markup. Since JSON references may be relative, it's crucial to
    # pass a document URI in order to properly resolve them.
    spec = _resolve_refs(options.get('uri', ''), spec)

    # OpenAPI spec may contain common endpoint's parameters top-level.
    # In order to do not place if-s around the code to handle special
    # cases, let's normalize the spec and push common parameters inside
    # endpoints definitions.
    for endpoint in spec['paths'].values():
        parameters = endpoint.pop('parameters', [])
        for method in endpoint.values():
            method.setdefault('parameters', [])
            method['parameters'].extend(parameters)


def openapi2httpdomain(spec, **options):
    generators = []

    # OpenAPI spec may contain JSON references, common properties, etc.
    # Trying to render the spec "As Is" will require to put multiple
    # if-s around the code. In order to simplify flow, let's make the
    # spec to have only one (expected) schema, i.e. normalize it.
    _normalize_spec(spec, **options)

    # If 'paths' are passed we've got to ensure they exist within an OpenAPI
    # spec; otherwise raise error and ask user to fix that.
    if 'paths' in options:
        if not set(options['paths']).issubset(spec['paths']):
            raise ValueError(
                'One or more paths are not defined in the spec: %s.' % (
                    ', '.join(set(options['paths']) - set(spec['paths'])),
                )
            )

    for endpoint in options.get('paths', spec['paths']):
        for method, properties in spec['paths'][endpoint].items():
            generators.append(_httpresource(endpoint, method, properties))

    return iter(itertools.chain(*generators))


class OpenApi(Directive):

    required_arguments = 1                  # path to openapi spec
    final_argument_whitespace = True        # path may contain whitespaces
    option_spec = {
        'encoding': directives.encoding,    # useful for non-ascii cases :)
        'paths': lambda s: s.split(),       # endpoints to be rendered
    }

    def run(self):
        env = self.state.document.settings.env
        relpath, abspath = env.relfn2path(directives.path(self.arguments[0]))

        # Add OpenAPI spec as a dependency to the current document. That means
        # the document will be rebuilt if the spec is changed.
        env.note_dependency(relpath)

        # Read the spec using encoding passed to the directive or fallback to
        # the one specified in Sphinx's config.
        encoding = self.options.get('encoding', env.config.source_encoding)
        with io.open(abspath, 'rt', encoding=encoding) as stream:
            spec = yaml.load(stream, _YamlOrderedLoader)

        # URI parameter is crucial for resolving relative references. So
        # we need to set this option properly as it's used later down the
        # stack.
        self.options.setdefault('uri', 'file://%s' % abspath)

        # reStructuredText DOM manipulation is pretty tricky task. It requires
        # passing dozen arguments which is not easy without well-documented
        # internals. So the idea here is to represent OpenAPI spec as
        # reStructuredText in-memory text and parse it in order to produce a
        # real DOM.
        viewlist = ViewList()
        for line in openapi2httpdomain(spec, **self.options):
            viewlist.append(line, '<openapi>')

        # Parse reStructuredText contained in `viewlist` and return produced
        # DOM nodes.
        node = nodes.section()
        node.document = self.state.document
        nested_parse_with_titles(self.state, viewlist, node)
        return node.children


def setup(app):
    app.setup_extension('sphinxcontrib.httpdomain')
    app.add_directive('openapi', OpenApi)
