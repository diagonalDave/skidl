# -*- coding: utf-8 -*-

# MIT license
#
# Copyright (C) 2018 by XESS Corp.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""
Handles complete circuits made of parts and nets.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

import os.path
import time
from builtins import range, str, super
from collections import defaultdict
import json
import subprocess

import graphviz
from future import standard_library

from .baseobj import SkidlBaseObject
from .Bus import Bus
from .defines import *
from .erc import dflt_circuit_erc
from .Interface import Interface
from .logger import erc_logger, logger
from .Net import NCNet, Net
from .Part import Part
from .Pin import Pin
from .pckg_info import __version__
from .SchLib import SchLib
from .scriptinfo import *
from .utilities import *

standard_library.install_aliases()

try:
    import __builtin__ as builtins
except ImportError:
    import builtins


class Circuit(SkidlBaseObject):
    """
    Class object that holds the entire netlist of parts and nets.

    Attributes:
        parts: List of all the schematic parts as Part objects.
        nets: List of all the schematic nets as Net objects.
        buses: List of all the buses as Bus objects.
        hierarchy: A '.'-separated concatenation of the names of nested
            SubCircuits at the current time it is read.
        level: The current level in the schematic hierarchy.
        context: Stack of contexts for each level in the hierarchy.
    """

    # Set the default ERC functions for all Circuit instances.
    erc_list = [dflt_circuit_erc]

    def __init__(self, **kwargs):
        super().__init__()

        """Initialize the Circuit object."""
        self.reset(init=True)

        # Each circuit instance has an individual set of assertions.
        erc_assertion_list = list()

        # Set passed-in attributes for the circuit.
        for k, v in list(kwargs.items()):
            setattr(self, k, v)

    def reset(self, init=False):
        """Clear any circuitry and cached part libraries and start over."""

        # Clear circuitry.
        self.mini_reset(init)

        # Also clear any cached libraries.
        SchLib.reset()
        global backup_lib
        backup_lib = None

    def mini_reset(self, init=False):
        """Clear any circuitry but don't erase any loaded part libraries."""

        self.name = ""
        self.parts = []
        self.nets = []
        self.netclasses = {}
        self.buses = []
        self.interfaces = []
        self.packages = []
        self.hierarchy = "top"
        self.level = 0
        self.context = [("top",)]
        self.erc_assertion_list = []
        self.no_files = False  # Allow creation of files for netlists, ERC, libs, etc.

        # Clear the name heap for nets and parts.
        reset_get_unique_name()

        # Clear out the no-connect net and set the global no-connect if it's
        # tied to this circuit.
        self.NC = NCNet(
            name="__NOCONNECT", circuit=self
        )  # Net for storing no-connects for parts in this circuit.
        if not init and self is default_circuit:
            builtins.NC = self.NC

    def add_parts(self, *parts):
        """Add some Part objects to the circuit."""
        for part in parts:
            # Add the part to this circuit if the part is movable and
            # it's not already in this circuit.
            if part.circuit != self:
                if part.is_movable():

                    # Remove the part from the circuit it's already in.
                    if isinstance(part.circuit, Circuit):
                        part.circuit -= part

                    # Add the part to this circuit.
                    part.circuit = self  # Record the Circuit object for this part.
                    part.ref = part.ref  # This adjusts the part reference if necessary.

                    part.hierarchy = self.hierarchy  # Store hierarchy of part.
                    part.skidl_trace = (
                        get_skidl_trace()
                    )  # Store part instantiation trace.

                    self.parts.append(part)
                else:
                    log_and_raise(
                        logger,
                        ValueError,
                        "Can't add unmovable part {} to this circuit.".format(part.ref),
                    )

    def rmv_parts(self, *parts):
        """Remove some Part objects from the circuit."""
        for part in parts:
            if part.is_movable():
                if part.circuit == self and part in self.parts:
                    part.circuit = None
                    part.hierarchy = None
                    self.parts.remove(part)
                else:
                    logger.warning(
                        "Removing non-existent part {} from this circuit.".format(
                            part.ref
                        )
                    )
            else:
                log_and_raise(
                    logger,
                    ValueError,
                    "Can't remove part {} from this circuit.".format(part.ref),
                )

    def add_nets(self, *nets):
        """Add some Net objects to the circuit. Assign a net name if necessary."""
        for net in nets:
            # Add the net to this circuit if the net is movable and
            # it's not already in this circuit.
            if net.circuit != self:
                if net.is_movable():

                    # Remove the net from the circuit it's already in.
                    if isinstance(net.circuit, Circuit):
                        net.circuit -= net

                    # Add the net to this circuit.
                    net.circuit = self  # Record the Circuit object the net belongs to.
                    net.name = net.name
                    net.hierarchy = self.hierarchy  # Store hierarchy of net.

                    self.nets.append(net)

                else:
                    log_and_raise(
                        logger,
                        ValueError,
                        "Can't add unmovable net {} to this circuit.".format(net.name),
                    )

    def rmv_nets(self, *nets):
        """Remove some Net objects from the circuit."""
        for net in nets:
            if net.is_movable():
                if net.circuit == self and net in self.nets:
                    net.circuit = None
                    net.hierarchy = None
                    self.nets.remove(net)
                else:
                    logger.warning(
                        "Removing non-existent net {} from this circuit.".format(
                            net.name
                        )
                    )
            else:
                log_and_raise(
                    logger,
                    ValueError,
                    "Can't remove unmovable net {} from this circuit.".format(net.name),
                )

    def add_buses(self, *buses):
        """Add some Bus objects to the circuit. Assign a bus name if necessary."""
        for bus in buses:
            # Add the bus to this circuit if the bus is movable and
            # it's not already in this circuit.
            if bus.circuit != self:
                if bus.is_movable():

                    # Remove the bus from the circuit it's already in, but skip
                    # this if the bus isn't already in a Circuit.
                    if isinstance(bus.circuit, Circuit):
                        bus.circuit -= bus

                    # Add the bus to this circuit.
                    bus.circuit = self
                    bus.name = bus.name
                    bus.hierarchy = self.hierarchy  # Store hierarchy of the bus.

                    self.buses.append(bus)
                    for net in bus.nets:
                        self += net

    def rmv_buses(self, *buses):
        """Remove some buses from the circuit."""
        for bus in buses:
            if bus.is_movable():
                if bus.circuit == self and bus in self.buses:
                    bus.circuit = None
                    bus.hierarchy = None
                    self.buses.remove(bus)
                    for net in bus.nets:
                        self -= net
                else:
                    logger.warning(
                        "Removing non-existent bus {} from this circuit.".format(
                            bus.name
                        )
                    )
            else:
                log_and_raise(
                    logger,
                    ValueError,
                    "Can't remove unmovable bus {} from this circuit.".format(bus.name),
                )

    def add_packages(self, *packages):
        self.packages.extend(packages)

    def rmv_packages(self, *packages):
        for package in packages:
            self.packages.remove(package)

    def add_stuff(self, *stuff):
        """Add Parts, Nets, Buses, and Interfaces to the circuit."""

        from .Package import Package

        for thing in flatten(stuff):
            if isinstance(thing, Part):
                self.add_parts(thing)
            elif isinstance(thing, Net):
                self.add_nets(thing)
            elif isinstance(thing, Bus):
                self.add_buses(thing)
            elif isinstance(thing, Package):
                self.add_packages(thing)
            else:
                log_and_raise(
                    logger,
                    ValueError,
                    "Can't add a {} to a Circuit object.".format(type(thing)),
                )
        return self

    def rmv_stuff(self, *stuff):
        """Remove Parts, Nets, Buses, and Interfaces from the circuit."""

        from .Package import Package

        for thing in flatten(stuff):
            if isinstance(thing, Part):
                self.rmv_parts(thing)
            elif isinstance(thing, Net):
                self.rmv_nets(thing)
            elif isinstance(thing, Bus):
                self.rmv_buses(thing)
            elif isinstance(thing, Package):
                self.rmv_packages(thing)
            else:
                log_and_raise(
                    logger,
                    ValueError,
                    "Can't remove a {} from a Circuit object.".format(type(pnb)),
                )
        return self

    __iadd__ = add_stuff
    __isub__ = rmv_stuff

    def get_nets(self):
        """Get all the distinct nets for the circuit."""

        distinct_nets = []
        for net in self.nets:
            if net is self.NC:
                # Exclude no-connect net.
                continue
            if not net.get_pins():
                # Exclude empty nets with no attached pins.
                continue
            for n in distinct_nets:
                # Exclude net if its already attached to a previously selected net.
                if net.is_attached(n):
                    break
            else:
                # This net is not attached to any of the other distinct nets,
                # so it is also distinct.
                distinct_nets.append(net)
        return distinct_nets

    def instantiate_packages(self):
        """Run the package executables to instantiate their circuitry."""
        for package in self.packages:
            package.subcircuit(**package)

        # Avoid duplicating circuitry by deleting packages after they've
        # been instantiated once.
        self.packages = []

    def ERC(self, *args, **kwargs):
        """Run class-wide and local ERC functions on this circuit."""

        # Generate circuitry for any packages that were instantiated.
        self.instantiate_packages()

        # Reset the counters to clear any warnings/errors from previous ERC run.
        erc_logger.error.reset()
        erc_logger.warning.reset()

        if self.no_files:
            erc_logger.stop_file_output()

        super().ERC(*args, **kwargs)

        if (erc_logger.error.count, erc_logger.warning.count) == (0, 0):
            sys.stderr.write("\nNo ERC errors or warnings found.\n\n")
        else:
            sys.stderr.write(
                "\n{} warnings found during ERC.\n".format(erc_logger.warning.count)
            )
            sys.stderr.write(
                "{} errors found during ERC.\n\n".format(erc_logger.error.count)
            )

    def _merge_net_names(self):
        """Select a single name for each multi-segment net."""

        for net in self.nets:
            net.merge_names()

    def generate_netlist(self, **kwargs):
        """
        Return a netlist and also write it to a file/stream.

        Args:
            file_: Either a file object that can be written to, or a string
                containing a file name, or None.
            tool: The EDA tool the netlist will be generated for.
            do_backup: If true, create a library with all the parts in the circuit.

        Returns:
            A netlist.
        """

        from . import skidl

        # Generate circuitry for any packages that were instantiated.
        self.instantiate_packages()

        # Reset the counters to clear any warnings/errors from previous run.
        logger.error.reset()
        logger.warning.reset()

        # Before anything else, clean-up names for multi-segment nets.
        self._merge_net_names()

        # Extract arguments:
        #     Get EDA tool the netlist will be generated for.
        #     Get file the netlist will be stored in (if any).
        #     Get flag controlling the generation of a backup library.
        tool = kwargs.pop("tool", skidl.get_default_tool())
        file_ = kwargs.pop("file_", None)
        do_backup = kwargs.pop("do_backup", True)

        try:
            gen_func = getattr(self, "_gen_netlist_{}".format(tool))
            netlist = gen_func(**kwargs)  # Pass any remaining arguments.
        except KeyError:
            log_and_raise(
                logger,
                ValueError,
                "Can't generate netlist in an unknown ECAD tool format ({}).".format(
                    tool
                ),
            )

        if (logger.error.count, logger.warning.count) == (0, 0):
            sys.stderr.write(
                "\nNo errors or warnings found during netlist generation.\n\n"
            )
        else:
            sys.stderr.write(
                "\n{} warnings found during netlist generation.\n".format(
                    logger.warning.count
                )
            )
            sys.stderr.write(
                "{} errors found during netlist generation.\n\n".format(
                    logger.error.count
                )
            )

        if not self.no_files:
            with opened(file_ or (get_script_name() + ".net"), "w") as f:
                f.write(str(netlist))

        if do_backup:
            self.backup_parts()  # Create a new backup lib for the circuit parts.
            global backup_lib  # Clear out any old backup lib so the new one
            backup_lib = None  #   will get reloaded when it's needed.

        return netlist

    def generate_xml(self, file_=None, tool=None):
        """
        Return netlist as an XML string and also write it to a file/stream.

        Args:
            file_: Either a file object that can be written to, or a string
                containing a file name, or None.

        Returns:
            A string containing the netlist.
        """

        from . import skidl

        # Generate circuitry for any packages that were instantiated.
        self.instantiate_packages()

        # Reset the counters to clear any warnings/errors from previous run.
        logger.error.reset()
        logger.warning.reset()

        # Clean-up names for multi-segment nets.
        self._merge_net_names()

        if tool is None:
            tool = skidl.get_default_tool()

        try:
            gen_func = getattr(self, "_gen_xml_{}".format(tool))
            netlist = gen_func()
        except KeyError:
            log_and_raise(
                logger,
                ValueError,
                "Can't generate XML in an unknown ECAD tool format ({}).".format(tool),
            )

        if (logger.error.count, logger.warning.count) == (0, 0):
            sys.stderr.write("\nNo errors or warnings found during XML generation.\n\n")
        else:
            sys.stderr.write(
                "\n{} warnings found during XML generation.\n".format(
                    logger.warning.count
                )
            )
            sys.stderr.write(
                "{} errors found during XML generation.\n\n".format(logger.error.count)
            )

        if not self.no_files:
            with opened(file_ or (get_script_name() + ".xml"), "w") as f:
                f.write(netlist)

        return netlist

    def generate_skin(self):
        part_svg = {}
        for part in self.parts:
            part_svg[part.name] = part.generate_svg_component()
        part_svg = "\n".join(part_svg.values())
        head_svg = """
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     xmlns:s="https://github.com/nturley/netlistsvg">
  <s:properties
    constants="false"
    splitsAndJoins="false"
    genericsLaterals="true">
    <s:layoutEngine
        org.eclipse.elk.layered.spacing.nodeNodeBetweenLayers="5"
        org.eclipse.elk.layered.compaction.postCompaction.strategy="4"
        org.eclipse.elk.spacing.nodeNode= "100"
        org.eclipse.elk.direction="DOWN"/>
  </s:properties>
<style>
svg {
  stroke: #000;
  fill: none;
  stroke-linejoin: round;
  stroke-linecap: round;
}
text {
  fill: #000;
  stroke: none;
  font-size: 10px;
  font-weight: bold;
  font-family: "Courier New", monospace;
}
.pin_text_up {
  text-anchor: middle;
}
.pin_text_down {
  text-anchor: middle;
}
.pin_text_right {
  text-anchor: end;
}
.pin_text_left {
  text-anchor: start;
}
.nodelabel {
  text-anchor: middle;
}
.inputPortLabel {
  text-anchor: end;
}
.splitjoinBody {
  fill: #000;
}
.symbol {
  stroke-linejoin: round;
  stroke-linecap: round;
}
.detail {
  stroke-linejoin: round;
  stroke-linecap: round;
  fill: #000;
}
.fill {
  fill: #000;
}
</style>

<!-- power -->
<g s:type="vcc" s:width="20" s:height="30" transform="translate(5,20)">
  <s:alias val="vcc" />
  <text x="10" y="-4" class="nodelabel $cell_id" s:attribute="name">name</text>
  <path d="M0,0 H20 L10,15 Z M10,15 V30" class="$cell_id"/>
  <g s:x="10" s:y="30" s:pid="A" s:position="bottom"/>
</g>

<g s:type="vee" s:width="20" s:height="30" transform="translate(40,35)">
	  <s:alias val="vee" />
	  <text x="10" y="10" class="nodelabel $cell_id" s:attribute="name">name</text>
	  <path d="M0,0 H20 L10,-15 Z M10,-15 V-30" class="$cell_id"/>
	  <g s:x="10" s:y="-30" s:pid="A" s:position="top"/>
	</g>

<g s:type="gnd" s:width="20" s:height="30" transform="translate(80,35)">
  <s:alias val="gnd"/>
  <text x="30" y="20" class="nodelabel $cell_id" s:attribute="name">name</text>
  <path d="M0,0 H20 M3,5 H17 M7,10 H13 M10,0 V-15" class="$cell_id"/>
  <g s:x="10" s:y="-15" s:pid="A" s:position="top"/>
</g>
<!-- power -->

<!-- signal -->
<g s:type="inputExt" s:width="30" s:height="20" transform="translate(0,0)">
  <text x="15" y="-4" class="$cell_id" s:attribute="ref">input</text>
  <s:alias val="$_inputExt_"/>
  <path d="M0,0 V20 H15 L30,10 15,0 Z" class="$cell_id"/>
  <g s:x="30" s:y="10" s:pid="Y" s:position="right"/>
</g>

<g s:type="outputExt" s:width="30" s:height="20" transform="translate(0,0)">
  <text x="15" y="-4" class="$cell_id" s:attribute="ref">output</text>
  <s:alias val="$_outputExt_"/>
  <path d="M30,0 V20 H15 L0,10 15,0 Z" class="$cell_id"/>
  <g s:x="0" s:y="10" s:pid="A" s:position="left"/>
</g>
<!-- signal -->

"""

        tail_svg ="""
<!-- builtin -->
<g s:type="generic" s:width="30" s:height="40" transform="translate(0,0)">
  <text x="15" y="-4" class="nodelabel $cell_id" s:attribute="ref">generic</text>
  <rect width="30" height="40" x="0" y="0" s:generic="body" class="$cell_id"/>
  <g transform="translate(30,10)"
     s:x="30" s:y="10" s:pid="out0" s:position="right">
    <text x="5" y="-4" class="$cell_id">out0</text>
  </g>
  <g transform="translate(30,30)"
     s:x="30" s:y="30" s:pid="out1" s:position="right">
    <text x="5" y="-4" class="$cell_id">out1</text>
  </g>
  <g transform="translate(0,10)"
     s:x="0" s:y="10" s:pid="in0" s:position="left">
      <text x="-3" y="-4" class="inputPortLabel $cell_id">in0</text>
  </g>
  <g transform="translate(0,30)"
     s:x="0" s:y="30" s:pid="in1" s:position="left">
    <text x="-3" y="-4" class="inputPortLabel $cell_id">in1</text>
  </g>
</g>
<!-- builtin -->

</svg>
"""        
        return head_svg + part_svg + tail_svg

    def generate_schematic(self, file_=None, tool=None):
        """
        Return a dictionary that can be displayed by netlistsvg.
        """

        from . import skidl

        # Generate circuitry for any packages that were instantiated.
        self.instantiate_packages()

        # Reset the counters to clear any warnings/errors from previous run.
        logger.error.reset()
        logger.warning.reset()

        # Clean-up names for multi-segment nets.
        self._merge_net_names()

        # Assign each net a unique integer identifier. Interconnected nets
        # all get the same number.
        net_nums = {}
        for num, net in enumerate(self.nets, 1):
            for n in net.get_nets():
                if n.name not in net_nums:
                    net_nums[n.name] = num

        ports = {}
        for net in self.nets:
            if not net.is_implicit():
                ports[net.name] = {
                    "direction": "input",
                    "bits": [net_nums[net.name],],
                }

        pin_dir = {
            Pin.types.INPUT: "input",
            Pin.types.OUTPUT: "output",
            Pin.types.BIDIR: "output",
            Pin.types.TRISTATE: "output",
            Pin.types.PASSIVE: "input",
            Pin.types.PULLUP: "output",
            Pin.types.PULLDN: "output",
            Pin.types.UNSPEC: "input",
            Pin.types.PWRIN: "input",
            Pin.types.PWROUT: "output",
            Pin.types.OPENCOLL: "output",
            Pin.types.OPENEMIT: "output",
            Pin.types.NOCONNECT: "input",
        }

        cells = {}
        for part in self.parts:
            try:
                connections = {
                    pin.num: [net_nums[pin.net.name],] for pin in part.get_pins()
                }
                port_directions = {
                    pin.num: pin_dir[pin.func] for pin in part.get_pins()
                }
                cells[part.ref] = {
                    "type": part.name,
                    "port_directions": port_directions,
                    "connections": connections,
                }
            except AttributeError:
                breakpoint()

        schematic_json = {"modules": {self.name: {"ports": ports, "cells": cells,}}}

        if not self.no_files:
            file_basename = file_ or get_script_name()
            json_file = file_basename + ".json"
            svg_file = file_basename + ".svg"
            with opened(json_file, "w") as f:
                f.write(
                    json.dumps(
                        schematic_json, sort_keys=True, indent=2, separators=(",", ": ")
                    )
                )
            skin_file = file_basename + "_skin.svg"
            with opened(skin_file, "w") as f:
                f.write(self.generate_skin())
            subprocess.call(
                "netlistsvg {json_file} --skin {skin_file} -o {svg_file}".format(
                    **locals()),
                shell=True,
            )

        return schematic_json

    def generate_graph(
        self,
        file_=None,
        engine="neato",
        rankdir="LR",
        part_shape="rectangle",
        net_shape="point",
        splines=None,
        show_values=True,
        show_anon=False,
        split_nets=["GND"],
        split_parts_ref=[],
    ):
        """
        Returns a graphviz graph as graphviz object and can also write it to a file/stream.
        When used in ipython the graphviz object will drawn as an SVG in the output.

        See https://graphviz.readthedocs.io/en/stable/ and http://graphviz.org/doc/info/attrs.html

        Args:
            file_: A string containing a file name, or None.
            engine: See graphviz documentation
            rankdir: See graphviz documentation
            part_shape: Shape of the part nodes
            net_shape: Shape of the net nodes
            splines: Style for the edges, try 'ortho' for a schematic like feel
            show_values: Show values as external labels on part nodes
            show_anon: Show anonymous net names
            split_nets: splits up the plot for the given list of net names
            split_parts_ref: splits up the plot for all pins for the given list of part refs

        Returns:
            graphviz.Digraph
        """

        # Generate circuitry for any packages that were instantiated.
        self.instantiate_packages()

        # Reset the counters to clear any warnings/errors from previous run.
        logger.error.reset()
        logger.warning.reset()

        # Before anything else, clean-up names for multi-segment nets.
        self._merge_net_names()

        dot = graphviz.Digraph(engine=engine)
        dot.attr(rankdir=rankdir, splines=splines)

        nets = self.get_nets()

        # try and keep things in the same order
        nets.sort(key=lambda n: n.name.lower())

        for i, n in enumerate(nets):
            xlabel = n.name
            if not show_anon and n.is_implicit():
                xlabel = None
            if n.name not in split_nets:
                dot.node(n.name, shape=net_shape, xlabel=xlabel)

            for j, pin in enumerate(n.get_pins()):
                net_ref = n.name
                pin_part_ref = pin.part.ref

                if n.name in split_nets:
                    net_ref += str(j)
                    dot.node(net_ref, shape=net_shape, xlabel=xlabel)
                if pin.part.ref in split_parts_ref and n.name not in split_nets:
                    label = pin.part.ref + ":" + pin.name

                    # add label to part
                    net_ref_part = "%s_%i_%i" % (net_ref, i, j)
                    dot.node(net_ref_part, shape=net_shape, xlabel=label)
                    dot.edge(pin_part_ref, net_ref_part, arrowhead="none")

                    # add label to splited net
                    pin_part_ref = "%s_%i_%i" % (pin_part_ref, i, j)
                    dot.node(pin_part_ref, shape=net_shape, xlabel=label)
                    dot.edge(pin_part_ref, net_ref, arrowhead="none")
                else:
                    dot.edge(
                        pin_part_ref, net_ref, arrowhead="none", taillabel=pin.name
                    )

        for p in sorted(self.parts, key=lambda p: p.ref.lower()):
            xlabel = None
            if show_values:
                xlabel = p.value
            dot.node(p.ref, shape=part_shape, xlabel=xlabel)

        if not self.no_files:
            if file_ is not None:
                dot.save(file_)

        return dot

    def backup_parts(self, file_=None):
        """
        Saves parts in circuit as a SKiDL library in a file.

        Args:
            file: Either a file object that can be written to, or a string
                containing a file name, or None. If None, a standard library
                file will be used.

        Returns:
            Nothing.
        """

        from . import skidl

        # Generate circuitry for any packages that were instantiated.
        self.instantiate_packages()

        lib = SchLib(tool=SKIDL)  # Create empty library.
        for p in self.parts:
            lib += p
        if not file_:
            file_ = skidl.BACKUP_LIB_FILE_NAME

        if not self.no_files:
            lib.export(libname=skidl.BACKUP_LIB_NAME, file_=file_)


__func_name_cntr = defaultdict(int)


def SubCircuit(f):
    """
    A @SubCircuit decorator is used to create hierarchical circuits.

    Args:
        f: The function containing SKiDL statements that represents a subcircuit.
    """

    def sub_f(*args, **kwargs):
        # Upon entry, save the reference to the current default Circuit object.
        save_default_circuit = default_circuit  # pylint: disable=undefined-variable

        # If the subcircuit uses the 'circuit' argument, then set the default
        # Circuit object to that. Otherwise, use the current default Circuit object.
        circuit = kwargs.pop("circuit", default_circuit)
        builtins.default_circuit = circuit

        # Setup some globals needed in the subcircuit.
        builtins.NC = default_circuit.NC  # pylint: disable=undefined-variable

        # Invoking the subcircuit function creates circuitry at a level one
        # greater than the current level. (The top level is zero.)
        circuit.level += 1

        # Create a name for this subcircuit from the concatenated names of all
        # the nested subcircuit functions that were called on all the preceding levels
        # that led to this one. Also, add a distinct integer to the current
        # function name to disambiguate multiple uses of the same function.
        circuit.hierarchy = (
            circuit.context[-1][0]
            + "."
            + f.__name__
            + str(__func_name_cntr[f.__name__])
        )
        __func_name_cntr[f.__name__] = __func_name_cntr[f.__name__] + 1

        # Store the context so it can be used if this subcircuit function
        # invokes another subcircuit function within itself to add more
        # levels of hierarchy.
        circuit.context.append((circuit.hierarchy,))

        # Call the function to create whatever circuitry it handles.
        # The arguments to the function are usually nets to be connected to the
        # parts instantiated in the function, but they may also be user-specific
        # and have no effect on the mechanics of adding parts or nets although
        # they may direct the function as to what parts and nets get created.
        # Store any results it returns as a list. These results are user-specific
        # and have no effect on the mechanics of adding parts or nets.
        results = f(*args, **kwargs)

        # Restore the context that existed before the subcircuitry was
        # created. This does not remove the circuitry since it has already been
        # added to the parts and nets lists.
        circuit.context.pop()

        # Restore the hierarchy label and level.
        circuit.hierarchy = circuit.context[-1][0]
        circuit.level -= 1

        # Restore the default circuit and globals.
        builtins.default_circuit = save_default_circuit
        builtins.NC = default_circuit.NC  # pylint: disable=undefined-variable

        return results

    return sub_f


# The decorator can also be called as "@subcircuit".
subcircuit = SubCircuit
