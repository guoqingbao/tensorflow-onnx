# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""
tf2onnx.rewriter.custom_rnn_rewriter - custom rnn support
"""

from __future__ import division
from __future__ import print_function
import logging
import sys
import traceback
from onnx import onnx_pb
import numpy as np
from tf2onnx.rewriter.loop_rewriter_base import LoopRewriterBase, Context
from tf2onnx.rewriter.rnn_utils import REWRITER_RESULT
from tf2onnx.tfonnx import utils


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tf2onnx.rewriter.custom_rnn_rewriter")

# pylint: disable=missing-docstring,invalid-name,unused-argument,using-constant-test,broad-except,protected-access


class CustomRnnContext(Context):
    def __init__(self):
        super(CustomRnnContext, self).__init__()
        self.rnn_scope = None
        self.time_var = None
        self.iteration_var = None


class CustomRnnRewriter(LoopRewriterBase):
    def create_context(self):
        return CustomRnnContext()

    def run(self):
        log.debug("enter custom rnn rewriter")
        return self.run_internal()

    def _get_rnn_scope_name(self, while_scope_name):
        parts = while_scope_name.split('/')
        rnn_scope = '/'.join(parts[0:-2]) + "/"
        log.debug("found rnn scope %s", rnn_scope)
        return rnn_scope

    def _parse_rnn_loop(self, context):
        # check a while loop is generated by dynamic_rnn or bidirectional_rnn by
        #
        # 1. some patterns in _time_step in dynamic_rnn: tensor array read, tensor array write
        # 2. some patterns in control_flow_ops.while_loop in dynamic_rnn:
        #      cond: time < loop_bound
        #      loop_vars: (time, output_ta, state)
        #      time has name called "time"
        #      iteration_cnt is added by control flow.

        # be noted:
        # 1. iteration counter does not exist in tf1.4 or earlier versions
        # 2. if dynamic_rnn's first input is not consumed, output ta does not exist.
        time_name = context.rnn_scope + "time"
        ta_array_name_prefix = context.rnn_scope + "dynamic_rnn/output_"
        iteration_counter_name = context.while_context_scope + "iteration_counter"

        found_time = False
        is_rnn_out_ta = None
        for val in context.loop_properties.all_variables.values():
            enter_input_node = self.g.get_node_by_output(val.enter_input_id)
            if val.is_tensor_array:
                ta_name = enter_input_node.get_attr("tensor_array_name").s.decode("utf-8")
                if not ta_name.startswith(ta_array_name_prefix):
                    is_rnn_out_ta = False
            elif enter_input_node.name == time_name:
                found_time = True
                context.time_var = val
            elif enter_input_node.name == iteration_counter_name:
                context.iteration_var = val


        if not found_time or is_rnn_out_ta is False:
            log.debug("this should not be a dynamic_rnn loop, found_time: %s, is_rnn_out_ta: %s",
                      found_time, is_rnn_out_ta)
            return False

        return True

    def need_rewrite(self, context):
        context.rnn_scope = self._get_rnn_scope_name(context.while_context_scope)

        if not self._parse_rnn_loop(context):
            log.debug("skip the loop due to parse_rnn_loop failed")
            return False

        self._parse_time_var(context)

        if not context.loop_properties.tensor_array_inputs:
            log.debug("this should not be a dynamic_rnn loop, no ta input is found")
            return False
        return True

    def rewrite(self, context):
        log.debug("enter rewrite function")
        try:
            scan_props = context.loop_properties

            state_inputs_initial_values = []
            for state_input in scan_props.state_inputs_initial_values:
                nodes = self._adapt_scan_sequence_input_or_output("input", state_input, False)
                state_inputs_initial_values.append(nodes[-1].output[0])

            scan_inputs_initial_values = []
            for scan_input in scan_props.scan_inputs_initial_values:
                nodes = self._adapt_scan_sequence_input_or_output("input", scan_input, False)
                scan_inputs_initial_values.append(nodes[-1].output[0])

            cell_g_info = context.cell_graph
            scan_body_g = LoopRewriterBase.construct_graph_from_nodes(self.g, cell_g_info.nodes, cell_g_info.outputs)
            for input_tensor_info in scan_props.state_inputs:
                scan_body_g.add_graph_input(input_tensor_info.id, input_tensor_info.dtype, input_tensor_info.shape)

            for input_tensor_info in scan_props.scan_inputs:
                scan_body_g.add_graph_input(input_tensor_info.id, input_tensor_info.dtype, input_tensor_info.shape)

            scan_node = self._create_scan_node(context, scan_props,
                                               state_inputs_initial_values + scan_inputs_initial_values)
            if not scan_node:
                log.error("failed to create scan node during rewrite")
                return REWRITER_RESULT.FAIL

            scan_node.set_body_graph_as_attr("body", scan_body_g)
            self._connect_scan_with_output(context, scan_node)

            return REWRITER_RESULT.OK

        except Exception as ex:
            tb = traceback.format_exc()
            log.error("custom rnn rewrite failed, due to exception: %s, details:%s", ex, tb)
            return REWRITER_RESULT.FAIL

    def _parse_time_var(self, context):
        time_var = context.time_var
        log.debug("time var %s - enter input id (%s) shape: %s, output (%s) shape: %s", time_var.enter_name,
                  time_var.enter_input_id, self.g.get_shape(time_var.enter_input_id),
                  time_var.switch_true_identity_output.id, time_var.switch_true_identity_output.shape)

    def _create_scan_node(self, context, scan_props, init_values):
        log.debug("create scan node")
        # reuse original output connection id (e.g. Exit_XXX), so we don't need set shape.
        loop_outputs_shapes = []
        loop_outputs_dtypes = []
        for tensor_value_info in scan_props.state_outputs_exits + scan_props.scan_outputs_exits:
            if tensor_value_info.id:
                loop_outputs_shapes.append([1] + tensor_value_info.shape)
                loop_outputs_dtypes.append(tensor_value_info.dtype)
                n = self.g.get_node_by_output(tensor_value_info.id)
                self.g.remove_node(n.name)
            else:
                loop_outputs_shapes.append(None)
                loop_outputs_dtypes.append(None)

        # here we did not give the sequence_length, because
        # current batch size is 1, not original batch size
        # original seq_length will be used by the loop body of Scan op.
        scan_node = self.g.make_node("Scan", [""] + init_values, op_name_scope="custom_rnn_scan",
                                     attr={"num_scan_inputs": len(scan_props.scan_inputs)},
                                     output_count=len(scan_props.state_outputs + scan_props.scan_outputs),
                                     shapes=loop_outputs_shapes, dtypes=loop_outputs_dtypes,
                                     skip_conversion=False)

        return scan_node

    def _connect_scan_with_output(self, context, scan_node):
        log.debug("connect scan output with the graph")

        index = 0
        for out_tensor_value_info in context.loop_properties.state_outputs_exits:
            if out_tensor_value_info.id:
                nodes = self._adapt_scan_sequence_input_or_output("state_output_reshape",
                                                                  scan_node.output[index], True)
                self.g.replace_all_inputs(self.g.get_nodes(), out_tensor_value_info.id, nodes[-1].output[0])

            index += 1

        for out_tensor_value_info in context.loop_properties.scan_outputs_exits:
            if out_tensor_value_info.id:
                nodes = self._adapt_scan_sequence_input_or_output("scan_output_reshape",
                                                                  scan_node.output[index], True)
                self.g.replace_all_inputs(self.g.get_nodes(), out_tensor_value_info.id, nodes[-1].output[0])
            index += 1


    def _adapt_scan_sequence_input_or_output(self, target_name, input_id, handle_output=False):
        nodes_to_add = []
        shape_node = self.g.make_node("Shape", [input_id])
        nodes_to_add.append(shape_node)
        inferred_shape = self.g.get_shape(input_id)
        if handle_output is True:
            # handle output:
            # if required dim values don't contain more than one -1,
            # just use a const for Reshape's shape input.
            if inferred_shape is not None and inferred_shape[1:].count(-1) <= 1:
                new_shape_node = self.g.make_const(utils.make_name(target_name + "_target_shape"),
                                                   np.array(inferred_shape[1:], dtype=np.int64))
                nodes_to_add.append(new_shape_node)
            else:
                # otherwise, get the dim dynamically, e.g. remove the fake batch size (e.g.1)
                # from [1, time, real-batch, ...]
                origin_shape_node = self.g.make_node("Cast", [shape_node.output[0]],
                                                     {"to": onnx_pb.TensorProto.FLOAT})
                nodes_to_add.append(origin_shape_node)

                sliced_shape_node = self.g.make_node("Slice", [origin_shape_node.output[0]],
                                                     {"axes": [0], "starts": [1], "ends": [sys.maxsize]})
                nodes_to_add.append(sliced_shape_node)

                new_shape_node = self.g.make_node("Cast", [sliced_shape_node.output[0]],
                                                  {"to": onnx_pb.TensorProto.INT64})
                nodes_to_add.append(new_shape_node)

            new_shape = inferred_shape[1:]
        else:
            # handle input:
            if inferred_shape is not None and inferred_shape.count(-1) <= 1:
                new_shape_node = self.g.make_const(utils.make_name(target_name + "_target_shape"),
                                                   np.array([1] + inferred_shape, dtype=np.int64))
                nodes_to_add.append(new_shape_node)
            else:
                # add a fake batch size : 1
                fake_batch_size_node = self.g.make_const(utils.make_name(target_name + "_target_shape"),
                                                         np.array([1,], dtype=np.int64))
                nodes_to_add.append(fake_batch_size_node)
                new_shape_node = self.g.make_node("Concat",
                                                  [fake_batch_size_node.output[0], shape_node.output[0]],
                                                  attr={"axis": 0})
                nodes_to_add.append(new_shape_node)
            new_shape = [1] + inferred_shape

        reshape_node = self.g.make_node("Reshape", [input_id, new_shape_node.output[0]],
                                        shapes=[new_shape],
                                        dtypes=[self.g.get_dtype(input_id)],
                                        op_name_scope=target_name)
        nodes_to_add.append(reshape_node)
        log.debug("create Reshape for scan output %s, with output shape %s",
                  reshape_node.output[0], new_shape)
        return nodes_to_add
