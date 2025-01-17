#!/usr/bin/env python

from loguru import logger
from traversome.utils import harmony_weights, run_dill_encoded  # WeightedGMMWithEM find_greatest_common_divisor,
from copy import deepcopy
from scipy.stats import norm
from collections import OrderedDict
import numpy as np
from numpy import log, exp, inf
from multiprocessing import Manager, Pool
import warnings
import dill
# import random


#suppress numpy warnings at exp()
warnings.filterwarnings('ignore')


class PathGenerator(object):
    """
    generate heuristic variants (isomers & sub-chromosomes) from alignments.
    Here the variants are not necessarily identical in contig composition.
    TODO automatically estimate num_search using convergence-test like approach
    """

    def __init__(self,
                 traversome_obj,
                 # assembly_graph,
                 # graph_alignment,
                 num_search=1000,
                 num_processes=1,
                 force_circular=True,
                 hetero_chromosome=True,
                 differ_f=1.,
                 decay_f=20.,
                 decay_t=1000,
                 cov_inert=1.,
                 use_alignment_cov=False,
                 min_unit_similarity=0.85):
        """
        :param traversome_obj: traversome object
        :param num_search:
        :param force_circular:
        :param hetero_chromosome:
        :param differ_f: difference factor [0, INF)
            Weighted by which, reads with the same overlap with current path will be used according to their counts.
            new_weights = (count_weights^differ_f)/sum(count_weights^differ_f)
            Zero leads to that the read counts play no effect in read choice.
        :param decay_f: decay factor [0, INF]
            Chance reduces by which, a read with less overlap with current path will be used to extend current path.
            probs_(N-m) = probs_(N) * decay_f^(-m)
            A large value leads to strictly following read paths.
        :param decay_t: decay threshold for number of reads [100, INF]
            Number of reads. Only reads that overlap most with current path will be considered in the extension.
            # also server as a cutoff version of decay_f
        :param cov_inert: coverage inertia [0, INF)
            The degree of tendency a path has to extend contigs with similar coverage.
            weight *= exp(-abs(log(extending_coverage/current_path_coverage)))^cov_inert
            Designed for the mixture of multiple-sourced genomes, e.g. plastome and mitogenome.
            Set to zero if the graph is a single-sourced graph.
        :param use_alignment_cov: use the coverage from assembly graph if False.
        :param min_unit_similarity: minimum contig-len-weighted path similarity shared among units [0.5, 1]
            Used to trigger the decomposition of a long path concatenated by multiple units.
        """
        assert 1 <= num_processes
        assert 0 <= differ_f
        assert 0 <= decay_f
        assert 100 <= decay_t
        assert 0 <= cov_inert
        assert 0.5 <= min_unit_similarity
        self.graph = traversome_obj.graph
        self.alignment = traversome_obj.alignment
        self.tvs = traversome_obj
        self.num_search = num_search
        self.num_processes = num_processes
        self.force_circular = force_circular
        self.hetero_chromosome = hetero_chromosome
        self.__differ_f = differ_f
        self.__decay_f = decay_f
        self.__decay_t = decay_t
        self.__cov_inert = cov_inert
        self.__random = traversome_obj.random
        self.__use_alignment_cov = use_alignment_cov
        self.__min_unit_similarity = min_unit_similarity

        # to be generated
        self.local_max_alignment_len = None
        self.read_paths = list()
        self.__read_paths_counter = dict()
        # self.__vertex_to_readpath = {vertex: set() for vertex in self.graph.vertex_info}
        self.__starting_subpath_to_readpaths = {}
        self.__middle_subpath_to_readpaths = {}
        self.__read_paths_counter_indexed = False
        self.contig_coverages = OrderedDict()
        # self.single_copy_vertices_prob = \
        #     OrderedDict([(_v, 1.) for _v in single_copy_vertices]) if single_copy_vertices \
        #         else OrderedDict()
        self.__candidate_single_copy_vs = set()
        self.variants = list()
        self.variants_counts = dict()

    def generate_heuristic_paths(self, num_processes=None):
        if num_processes is None:  # use the user input value if provided
            num_processes = self.num_processes
        assert num_processes >= 1
        logger.info("generating heuristic variants .. ")
        if not self.__read_paths_counter_indexed:
            self.index_readpaths_subpaths()
        if self.__use_alignment_cov:
            logger.debug("estimating contig coverages from read paths ..")
            self.estimate_contig_coverages_from_read_paths()
            # TODO: remove low coverage contigs
        else:
            self.use_contig_coverage_from_assembly_graph()
        self.estimate_single_copy_vertices()
        logger.debug("start traversing ..")
        if num_processes == 1:
            self.__gen_heuristic_paths_uni()
        else:
            self.__gen_heuristic_paths_mp(num_proc=num_processes)

    # def generate_heuristic_circular_isomers(self):
    #     # based on alignments
    #     logger.warning("This function is under testing .. ")
    #     if not self.__read_paths_counter_indexed:
    #         self.index_readpaths_subpaths()
    #
    ## different from PathGeneratorGraphOnly.get_all_circular_isomers()
    ## this seaching is within the scope of long reads-supported paths
    # def generate_all_circular_isomers(self):
    #     # based on alignments

    def index_readpaths_subpaths(self, filter_by_graph=True):
        self.__read_paths_counter = dict()
        alignment_lengths = []
        if filter_by_graph:
            for gaf_record in self.alignment:
                this_read_path = tuple(self.graph.get_standardized_path(gaf_record.path))
                # summarize only when the graph contain the path
                if self.graph.contain_path(this_read_path):
                    if this_read_path in self.__read_paths_counter:
                        self.__read_paths_counter[this_read_path] += 1
                    else:
                        self.__read_paths_counter[this_read_path] = 1
                        self.read_paths.append(this_read_path)
                    # record alignment length
                    alignment_lengths.append(gaf_record.p_align_len)
        else:
            for gaf_record in self.alignment:
                this_read_path = tuple(self.graph.get_standardized_path(gaf_record.path))
                if this_read_path in self.__read_paths_counter:
                    self.__read_paths_counter[this_read_path] += 1
                else:
                    self.__read_paths_counter[this_read_path] = 1
                    self.read_paths.append(this_read_path)
                # record alignment length
                alignment_lengths.append(gaf_record.p_align_len)
        for read_id, this_read_path in enumerate(self.read_paths):
            read_contig_num = len(this_read_path)
            forward_read_path_tuple = tuple(this_read_path)
            reverse_read_path_tuple = tuple(self.graph.reverse_path(this_read_path))
            for sub_contig_num in range(1, read_contig_num):
                # index the starting subpaths
                self.__index_start_subpath(forward_read_path_tuple[:sub_contig_num], read_id, True)
                # reverse
                self.__index_start_subpath(reverse_read_path_tuple[: sub_contig_num], read_id, False)
                # index the middle subpaths
                # excluding the start and the end subpaths: range(0 + 1, read_contig_num - sub_contig_num + 1 - 1)
                for go_sub in range(1, read_contig_num - sub_contig_num):
                    # forward
                    self.__index_middle_subpath(
                        forward_read_path_tuple[go_sub: go_sub + sub_contig_num], read_id, True)
                    # reverse
                    self.__index_middle_subpath(
                        reverse_read_path_tuple[go_sub: go_sub + sub_contig_num], read_id, False)
        #
        self.local_max_alignment_len = sorted(alignment_lengths)[-1]
        self.__read_paths_counter_indexed = True

    def estimate_contig_coverages_from_read_paths(self):
        """
        Counting the contig coverage using the occurrences in the read paths.
        Note: this will proportionally overestimate the coverage values comparing to base coverage values,
        """
        self.contig_coverages = OrderedDict([(v_name, 0) for v_name in self.graph.vertex_info])
        for read_path in self.read_paths:
            for v_name, v_end in read_path:
                if v_name in self.contig_coverages:
                    self.contig_coverages[v_name] += 1

    def use_contig_coverage_from_assembly_graph(self):
        self.contig_coverages = \
            OrderedDict([(v_name, self.graph.vertex_info[v_name].cov) for v_name in self.graph.vertex_info])

    def estimate_single_copy_vertices(self):
        """
        Currently only use the connection information

        TODO: use estimate variant separation and multiplicity estimation to better estimate this
        """
        for go_v, v_name in enumerate(self.graph.vertex_info):
            if len(self.graph.vertex_info[v_name].connections[True]) < 2 and \
                    len(self.graph.vertex_info[v_name].connections[False]) < 2:
                self.__candidate_single_copy_vs.add(v_name)

    #     np.random.seed(self.__random.randint(1, 10000))
    #     clusters_res = WeightedGMMWithEM(
    #         data_array=list(self.contig_coverages.values()),
    #         data_weights=[self.graph.vertex_info[v_name].len for v_name in self.graph.vertex_info]).run()
    #     mu_list = [params["mu"] for params in clusters_res["parameters"]]
    #     smallest_label = mu_list.index(min(mu_list))
    #     # self.contig_coverages[v_name],
    #     # loc = current_names[v_name] * old_cov_mean,
    #     # scale = old_cov_std
    #     if len(mu_list) == 1:
    #         for go_v, v_name in enumerate(self.graph.vertex_info):
    #             # looking for smallest vertices
    #             if clusters_res["labels"][go_v] == smallest_label:
    #                 if len(self.graph.vertex_info[v_name].connections[True]) < 2 and \
    #                         len(self.graph.vertex_info[v_name].connections[False]) < 2:
    #                     self.single_copy_vertices_prob[v_name] #
    #     else:

    def __gen_heuristic_paths_uni(self):
        """
        single-process version of generating heuristic paths
        """
        count_search = 0
        count_valid = 0
        # logger.info("Start generating candidate paths ..")
        # v_len = len(self.graph.vertex_info)
        while count_valid < self.num_search:
            new_path = self.get_single_traversal()
            count_search += 1
            logger.debug("    traversal {}: {}".format(count_search, self.graph.repr_path(new_path)))
            # logger.trace("  {} unique paths in {}/{} valid paths, {} traversals".format(
            #     len(self.variants), count_valid, self.num_search, count_search))

            is_circular_p = self.graph.is_circular_path(new_path)
            invalid_search = (self.force_circular and not is_circular_p) or \
                             (not self.hetero_chromosome and not self.graph.is_fully_covered_by(new_path))

            # Tested to be a bad idea
            # # the searching has been forced to be running until a circular result was found,
            # # so self.graph.is_circular_path(new_path)) is no more needed
            # invalid_search = not self.hetero_chromosome and not self.graph.is_fully_covered_by(new_path)

            if invalid_search:
                continue
            else:
                # if len(new_path) >= v_len * 2:  # using path length to guess multiple units is not a good idea
                if is_circular_p:
                    new_path_list = self.__decompose_hetero_units(new_path)
                else:
                    new_path_list = [new_path]
                for new_path in new_path_list:
                    count_valid += 1
                    if new_path in self.variants_counts:
                        self.variants_counts[new_path] += 1
                        logger.debug("  {} unique paths in {}/{} valid paths, {} traversals".format(
                            len(self.variants), count_valid, self.num_search, count_search))
                    else:
                        self.variants_counts[new_path] = 1
                        self.variants.append(new_path)
                        logger.info("  {} unique paths in {}/{} valid paths, {} traversals".format(
                            len(self.variants), count_valid, self.num_search, count_search))
                    if count_valid == self.num_search:
                        break
        logger.info("  {} unique paths in {}/{} valid paths, {} traversals".format(
            len(self.variants), count_valid, self.num_search, count_search))

    # TODO: modularize each traversal process as an independent run, communicating via files
    def __heuristic_traversal_worker(self, variant, variants_counts, g_vars, lock, event):
        """
        single worker of traversal, called by self.get_heuristic_paths_multiprocessing
        starting a new process from dill dumped python object: slow
        """
        while g_vars.count_valid < self.num_search:
            # move the parallelizable code block before the lock
            # <<<
            new_path = self.get_single_traversal()
            repr_path = self.graph.repr_path(new_path)
            is_circular_p = self.graph.is_circular_path(new_path)
            invalid_search = (self.force_circular and not is_circular_p) or \
                             (not self.hetero_chromosome and not self.graph.is_fully_covered_by(new_path))
            if not invalid_search:
                # if len(new_path) >= v_len * 2:  # using path length to guess multiple units is not a good idea
                if is_circular_p:
                    new_path_list = self.__decompose_hetero_units(new_path)
                else:
                    new_path_list = [new_path]
            else:
                new_path_list = []
            # >>>
            # locking the counts and variants
            lock.acquire()
            g_vars.count_search += 1
            logger.trace("    traversal {}: {}".format(g_vars.count_search, repr_path))
            if invalid_search:
                lock.release()
                continue
            else:
                for new_path in new_path_list:
                    if g_vars.count_valid >= self.num_search:
                        # kill all other workers
                        event.set()
                        break
                    else:
                        g_vars.count_valid += 1
                        if new_path in variants_counts:
                            variants_counts[new_path] += 1
                            logger.trace("  {} unique paths in {}/{} valid paths, {} traversals".format(
                                len(variant), g_vars.count_valid, self.num_search, g_vars.count_search))
                        else:
                            variants_counts[new_path] = 1
                            variant.append(new_path)
                            logger.info("  {} unique paths in {}/{} valid paths, {} traversals".format(
                                len(variant), g_vars.count_valid, self.num_search, g_vars.count_search))
                lock.release()
        # TODO: kill all other workers

    def __gen_heuristic_paths_mp(self, num_proc=2):
        """
        multiprocess version of generating heuristic paths
        starting a new process from dill dumped python object: slow
        """
        manager = Manager()
        variants_counts = manager.dict()
        variants = manager.list()
        global_vars = manager.Namespace()
        global_vars.count_search = 0
        global_vars.count_valid = 0
        lock = manager.Lock()
        event = manager.Event()
        # v_len = len(self.graph.vertex_info)
        pool_obj = Pool(processes=num_proc)  # the worker processes are daemonic
        # dump function and args
        logger.info("Serializing the heuristic searching for multiprocessing ..")
        payload = dill.dumps((self.__heuristic_traversal_worker,
                              (variants, variants_counts, global_vars, lock, event)))
        # logger.info("Start generating candidate paths ..")
        jobs = []
        for g_p in range(num_proc):
            logger.debug("assigning job to worker {}".format(g_p + 1))
            jobs.append(pool_obj.apply_async(run_dill_encoded, (payload,)))
            logger.debug("assigned job to worker {}".format(g_p + 1))
            if global_vars.count_valid >= self.num_search:
                break
        for job in jobs:
            job.get()  # tracking errors
        pool_obj.close()
        # logger.info("waiting ..")
        event.wait()
        pool_obj.terminate()
        pool_obj.join()  # maybe no need to join
        self.variants_counts = dict(variants_counts)
        self.variants = list(variants)
        logger.info("  {} unique paths in {}/{} valid paths, {} traversals".format(
            len(self.variants), global_vars.count_valid, self.num_search, global_vars.count_search))

    def __decompose_hetero_units(self, circular_path):
        """
        Decompose a path that may be composed of multiple circular paths (units) containing similar variants
        e.g. 1,2,3,4,5,1,-3,-2,4,5 was composed of 1,2,3,4,5 and 1,-3,-2,4,5,
             when all contigs were likely to be single copy
        e.g. 1,2,3,2,3,7,5,1,-3,-2,-3,-2,8,5 was composed of 1,2,3,2,3,7,5 and 1,-3,-2,-3,-2,8,5,
             when 1,5 were likely to be single copy
        """
        len_total = len(circular_path)
        if len_total < 4:
            return [circular_path]

        # 1.1 get the multiplicities (copy) information of (v_name, v_end) in the circular path
        logger.trace("circular_path: {}".format(circular_path))
        unique_vne_list = sorted(set(circular_path))
        copy_to_vne = OrderedDict()
        vne_to_copy = OrderedDict()
        for v_n_e in unique_vne_list:
            this_copy = circular_path.count(v_n_e)
            if this_copy not in copy_to_vne:
                copy_to_vne[this_copy] = []
            copy_to_vne[this_copy].append(v_n_e)
            vne_to_copy[v_n_e] = this_copy
        # 1.2 store v lengths
        v_lengths = OrderedDict([(v_n_, self.graph.vertex_info[v_n_].len) for v_n_, v_e_ in unique_vne_list])

        # 2. estimate candidate number of units.
        # The shared contig-len-weighted path should be larger than self.__min_unit_similarity
        candidate_sc_vertices = set([_v_n for _v_n, _v_e in vne_to_copy]) & self.__candidate_single_copy_vs
        logger.trace("      candidate_sc_vertices: {}".format(candidate_sc_vertices))
        copies = sorted(copy_to_vne)
        if candidate_sc_vertices:
            # limit the estimation to the candidate single copy vertices
            sum_lens = [sum([v_lengths[_v_n]
                             for _v_n, _v_e in copy_to_vne[copy_num] if _v_n in self.__candidate_single_copy_vs])
                        for copy_num in copies]
        else:
            # no candidate single copy vertices were present in the path, use all vertices (contigs)
            sum_lens = [sum([v_lengths[_v_n]
                             for _v_n, _v_e in copy_to_vne[copy_num]])
                        for copy_num in copies]
        weights = [_c * _l for _c, _l in zip(copies, sum_lens)]  # contig-len-weights
        sum_w = float(sum(weights))  # total weights
        weights = [_w / sum_w for _w in weights]   # percent of each copy-num set of contigs
        count_weights = len(weights)
        candidate_num_units = []
        for go_c, copy_num in enumerate(copies):
            if copy_num > 1:
                accumulated_weight = 0
                for go_w in range(go_c, count_weights):
                    # e.g. a fourfold contig may potentially contribute to the unit_similarity if the num of units is 2.
                    if copies[go_w] % copy_num == 0:
                        accumulated_weight += weights[go_w]
                if accumulated_weight >= self.__min_unit_similarity:
                    # append all candidate copy numbers here, no need to do prime factor
                    candidate_num_units.append(copy_num)

        # 3. try to decompose
        logger.trace("      candidate_num_units: {}".format(candidate_num_units))
        if not candidate_num_units:  # or candidate_num_units == [1]:
            return [circular_path]
        else:
            v_lengths = np.array(list(v_lengths.values()))
            v_copies = np.array(list(vne_to_copy.values()))
            # not very important TODO account for overlap; mutable overlaps; overlap = self.graph.overlap()
            total_base_len = float(sum(v_lengths * v_copies))  # ignore overlap effect
            qualified_schemes = set()
            for num_units in candidate_num_units:
                logger.trace("      num_units: {}".format(num_units))
                unit_sc_vertices = [v_n_e for v_n_e in copy_to_vne[num_units] if v_n_e[0] in candidate_sc_vertices]
                logger.trace("      unit_sc_vertices: {}".format(unit_sc_vertices))
                candidate_starts = unit_sc_vertices if unit_sc_vertices else copy_to_vne[num_units]
                logger.trace("      candidate_starts: {}".format(candidate_starts))
                # 3.1. only keep the first start_n_e for a series of consecutive start_n_e
                #      because they generated the same circular units
                sne_indices = {s_n_e: [] for s_n_e in candidate_starts}
                for s_id, s_ne in enumerate(circular_path):
                    if s_ne in sne_indices:
                        sne_indices[s_ne].append(s_id)
                sne_indices = [[s_n_e] + sne_indices[s_n_e] for s_n_e in candidate_starts]
                # for start_n_e in candidate_starts:
                #     sne_indices.append([start_n_e])
                #     sne_indices[-1].extend([_id for _id, _ne in enumerate(circular_path) if _ne == start_n_e])
                sne_indices.sort(key=lambda x: x[1:])  # sort by the indices
                # 3.1.1 the start
                for first_id, end_id in zip(sne_indices[0][2:] + sne_indices[0][1:2], sne_indices[-1][1:]):
                    if (end_id + 1) % len_total != first_id:
                        consecutive_ends = False
                        break
                else:
                    consecutive_ends = True
                # 3.1.2
                go_keep_start = 0
                go_check_start = 1
                step = 1
                while go_check_start < len(sne_indices):
                    for k_id, c_id in zip(sne_indices[go_keep_start][1:], sne_indices[go_check_start][1:]):
                        if (k_id + step) % len_total != c_id:
                            go_keep_start = go_check_start
                            go_check_start += 1
                            step = 1
                            break
                    else:
                        del sne_indices[go_check_start]
                        step += 1
                if consecutive_ends and len(sne_indices) > 1:
                    # sne_indices[0] and sne_indices[-1] will decompose the circular_path into the same units
                    del sne_indices[0]
                logger.trace("      sne_indices: {}".format(sne_indices))

                # 3.2 try to decompose and calculate the shared variants
                #     to determine whether those starts are qualified
                #     to break the original path into units
                for start_n_e, *s_indices in sne_indices:
                    units = []
                    for from_id, to_id in zip(s_indices[:-1], s_indices[1:]):
                        units.append(circular_path[from_id:to_id])
                    units.append(circular_path[s_indices[-1]:] + circular_path[:s_indices[0]])
                    variant_counts = np.array([[_unit.count(v_n_e_)
                                                for v_n_e_ in unique_vne_list]
                                                for _unit in units])
                    # idx_shared = (variant_counts == variant_counts[0]).all(axis=0)
                    variants_shared = variant_counts.min(axis=0)
                    # logger.info("idx_shared ({}): {}".format(len(idx_shared), idx_shared))
                    # logger.info("variant_counts[0] ({}): {}".format(len(variant_counts[0]), variant_counts[0]))
                    # logger.info("v_lengths ({}): {}".format(len(v_lengths), v_lengths))
                    shared_len = \
                        num_units * sum(variants_shared * v_lengths) / total_base_len
                    logger.trace("      shared_len: {}".format(shared_len))
                    if shared_len > self.__min_unit_similarity:
                        this_scheme = tuple(sorted([self.graph.get_standardized_path_circ(self.graph.roll_path(_unit))
                                                    for _unit in units]))
                        if this_scheme not in qualified_schemes:
                            qualified_schemes.add(this_scheme)
                            logger.trace("new scheme added: {}".format(this_scheme))
            logger.trace("      qualified_schemes ({}): {}".format(len(qualified_schemes), qualified_schemes))

            # 3.3 calculate the support from read paths
            circular_units = []
            original_sub_paths = set(self.tvs.get_variant_sub_paths(circular_path))
            for this_scheme in qualified_schemes:
                these_sub_paths = set()
                for this_unit in this_scheme:
                    these_sub_paths |= set(self.tvs.get_variant_sub_paths(this_unit))
                if original_sub_paths - these_sub_paths:
                    # the original one contains unique subpath(s)
                    continue
                else:
                    for this_unit in this_scheme:
                        circular_units.append(this_unit)
                    # calculate the multiplicity-based likelihood will be weird,
                    # because if we believe the decomposed units are a reasonable scheme,
                    # the graph itself is a mixture of combination.
                    # Besides, the multiplicity-based likelihood would definitely prefer the decomposed ones,
                    # given that the candidate_num_units is generated from self.__candidate_single_copy_vs if available
            logger.trace("      circular_units ({}): {}".format(len(circular_units), circular_units))
            if not circular_units:
                return [circular_path]
            else:
                return circular_units

    # def __decompose_hetero_units_old(self, circular_path):
    #     """
    #     Decompose a path that may be composed of multiple circular paths (units), which shared the same variants
    #     e.g. 1,2,3,4,5,1,-3,-2,4,5 was composed of 1,2,3,4,5 and 1,-3,-2,4,5
    #     """
    #     def get_v_counts(_path): return [_path.count(_v_name) for _v_name in self.graph.vertex_info]
    #     v_list = [v_name for v_name, v_end in circular_path]
    #     v_counts = get_v_counts(v_list)
    #     gcd = find_greatest_common_divisor(v_counts)
    #     logger.trace("  checking gcd {} from {}".format(gcd, circular_path))
    #     if gcd == 1:
    #         # the greatest common divisor is 1
    #         return [circular_path]
    #     else:
    #         logger.debug("  decompose {}".format(circular_path))
    #         v_to_id = {v_name: go_id for go_id, v_name in enumerate(self.graph.vertex_info)}
    #         unit_counts = [int(v_count/gcd) for v_count in v_counts]
    #         unit_len = int(len(v_list) / gcd)
    #         reseed_at = self.__random.randint(0, unit_len - 1)
    #         v_list_shuffled = v_list[len(v_list) - reseed_at:] + v_list + v_list[:unit_len]
    #         counts_check = get_v_counts(v_list_shuffled[:unit_len])
    #         find_start = False
    #         try_start = 0
    #         for try_start in range(unit_len):
    #             # if each unit has the same composition
    #             if counts_check == unit_counts and \
    #                     set([get_v_counts(v_list_shuffled[try_start+unit_len*go_u:try_start + unit_len*(go_u + 1)])
    #                          == unit_counts
    #                          for go_u in range(1, gcd)]) \
    #                     == {True}:
    #                 find_start = True
    #                 break
    #             else:
    #                 counts_check[v_to_id[v_list_shuffled[try_start]]] -= 1
    #                 counts_check[v_to_id[v_list_shuffled[try_start + unit_len]]] += 1
    #         if find_start:
    #             path_shuffled = circular_path[len(v_list) - reseed_at:] + circular_path + circular_path[:unit_len]
    #             unit_seq_len = self.graph.get_path_length(path_shuffled[try_start: try_start + unit_len])
    #             unit_copy_num = min(max(int((self.local_max_alignment_len - 2) / unit_seq_len), 1), gcd)
    #             return_list = []
    #             for go_unit in range(int(gcd/unit_copy_num)):
    #                 go_from__ = try_start + unit_len * unit_copy_num * go_unit
    #                 go_to__ = try_start + unit_len * unit_copy_num * (go_unit + 1)
    #                 variant_path = path_shuffled[go_from__: go_to__]
    #                 if self.graph.is_circular_path(variant_path):
    #                     return_list.append(self.graph.get_standardized_path_circ(variant_path))
    #             return return_list
    #         else:
    #             return [circular_path]

    def get_single_traversal(self):
        return self.graph.get_standardized_path_circ(self.graph.roll_path(self.__heuristic_extend_path([])))

    def __heuristic_extend_path(
            self, path, not_do_reverse=False):
        """
        TODO minimum requirement: make sure all read paths have been covered
        improvement needed
        :param path: empty path like [] or starting path like [("v1", True), ("v2", False)]
        :param not_do_reverse: mainly for iteration, a mark to stop searching from the reverse end
        :return: a candidate variant path. e.g. [("v0", True), ("v1", True), ("v2", False), ("v3", True)]
        """
        if not path:
            # randomly choose the read path and the direction
            # change the weight (-~ depth) to flatten the search
            read_p_freq_reciprocal = [1. / self.__read_paths_counter[r_p] for r_p in self.read_paths]
            read_path = self.__random.choices(self.read_paths, weights=read_p_freq_reciprocal)[0]
            if self.__random.random() > 0.5:
                read_path = self.graph.reverse_path(read_path)
            path = list(read_path)
            logger.trace("      starting path(" + str(len(path)) + "): " + str(path))
            # initial_mean, initial_std = self.__get_cov_mean(read_path, return_std=True)
            return self.__heuristic_extend_path(
                path=path,
                not_do_reverse=False)
        else:
            # keep going in a circle util the path length reaches beyond the longest read alignment
            # stay within what data can tell
            repeating_unit = self.graph.roll_path(path)
            if len(path) > len(repeating_unit) and \
                    self.graph.get_path_internal_length(path) >= self.local_max_alignment_len:
                logger.trace("      traversal ended within a circle unit.")
                return deepcopy(repeating_unit)
            #
            current_ave_coverage = self.__get_cov_mean(path)
            # generate the extending candidates
            candidate_ls_list = []
            candidates_list_overlap_c_nums = []
            for overlap_c_num in range(1, len(path) + 1):
                overlap_path = path[-overlap_c_num:]
                # stop adding extending candidate when the overlap is longer than our longest read alignment
                # stay within what data can tell
                if self.graph.get_path_internal_length(list(overlap_path) + [("", True)]) \
                        >= self.local_max_alignment_len:
                    break
                overlap_path = tuple(overlap_path)
                if overlap_path in self.__starting_subpath_to_readpaths:
                    # logger.debug("starting, " + str(self.__starting_subpath_to_readpaths[overlap_path]))
                    candidate_ls_list.append(sorted(self.__starting_subpath_to_readpaths[overlap_path]))
                    candidates_list_overlap_c_nums.append(overlap_c_num)
            # logger.debug(candidate_ls_list)
            # logger.debug(candidates_list_overlap_c_nums)
            if not candidate_ls_list:
                # if no extending candidate based on starting subpath (from one end),
                # try to simultaneously extend both ends from middle
                path_tuple = tuple(path)
                if path_tuple in self.__middle_subpath_to_readpaths:
                    candidates = sorted(self.__middle_subpath_to_readpaths[path_tuple])
                    weights = [self.__read_paths_counter[self.read_paths[read_id]] for read_id, strand in candidates]
                    weights = harmony_weights(weights, diff=self.__differ_f)
                    if self.__cov_inert:
                        cdd_cov = [self.__get_cov_mean(self.read_paths[read_id], exclude_path=path)
                                   for read_id, strand in candidates]
                        weights = [exp(log(weights[go_c])-abs(log(cov/current_ave_coverage)))
                                   for go_c, cov in enumerate(cdd_cov)]
                    read_id, strand = self.__random.choices(candidates, weights=weights)[0]
                    if strand:
                        path = list(self.read_paths[read_id])
                    else:
                        path = list(self.graph.reverse_path(self.read_paths[read_id]))
                    return self.__heuristic_extend_path(path)
            # like_ls_cached will be calculated in if not self.hetero_chromosome
            # it may be further used in self.__heuristic_check_multiplicity()
            like_ls_cached = []
            if not candidate_ls_list:
                # if no extending candidates based on overlap info, try to extend based on the graph
                logger.trace("      no extending candidates based on overlap info, try extending based on the graph")
                last_name, last_end = path[-1]
                next_connections = self.graph.vertex_info[last_name].connections[last_end]
                logger.trace("      {}, {}: next_connections: {}".format(last_name, last_end, next_connections))
                if next_connections:
                    if len(next_connections) > 1:
                        candidates_next = sorted(next_connections)
                        logger.trace("      candidates_next: {}".format(candidates_next))
                        if not self.hetero_chromosome:
                            # weighting candidates by the likelihood change of the multiplicity change
                            old_cov_mean, old_cov_std = self.__get_cov_mean(path, return_std=True)
                            logger.trace("      path mean:" + str(old_cov_mean) + "," + str(old_cov_std))
                            single_cov_mean, single_cov_std = self.__get_cov_mean_of_single(path, return_std=True)
                            logger.trace("      path single mean:" + str(single_cov_mean) + "," + str(single_cov_std))
                            current_vs = [v_n for v_n, v_e in path]
                            weights = []
                            for next_v in candidates_next:
                                v_name, v_end = next_v
                                current_v_counts = {v_name: current_vs.count(v_name)}
                                loglike_ls = self.__cal_multiplicity_like(path=deepcopy(path),
                                                                          proposed_extension=[next_v],
                                                                          current_v_counts=current_v_counts,
                                                                          old_cov_mean=old_cov_mean,
                                                                          old_cov_std=old_cov_std,
                                                                          single_cov_mean=single_cov_mean,
                                                                          single_cov_std=single_cov_std,
                                                                          logarithm=True)
                                like_ls_cached.append(exp(loglike_ls))
                                weights.append(loglike_ls[0])
                            logger.trace("      likes: {}".format(weights))
                            weights = exp(np.array(weights) - max(weights))
                            chosen_cdd_id = self.__random.choices(range(len(candidates_next)), weights=weights)[0]
                            next_name, next_end = candidates_next[chosen_cdd_id]
                            like_ls_cached = like_ls_cached[chosen_cdd_id]
                        elif self.__cov_inert:
                            # coverage inertia (multi-chromosomes) and not hetero_chromosome are mutually exclusive
                            # coverage inertia, more likely to extend to contigs with similar depths,
                            # which are more likely to be the same target chromosome / organelle type
                            cdd_cov = [self.contig_coverages[_n_] for _n_, _e_ in candidates_next]
                            weights = [exp(-abs(log(cov/current_ave_coverage))) for cov in cdd_cov]
                            logger.trace("      likes: {}".format(weights))
                            next_name, next_end = self.__random.choices(candidates_next, weights=weights)[0]
                        else:
                            next_name, next_end = self.__random.choice(candidates_next)
                    else:
                        next_name, next_end = list(next_connections)[0]
                        logger.trace("      single next: ({}, {})".format(next_name, next_end))
                        like_ls_cached = None
                    # if self.hetero_chromosome or self.graph.is_fully_covered_by(path + [(next_name, not next_end)]):
                    return self.__heuristic_check_multiplicity(
                        path=path,
                        proposed_extension=[(next_name, not next_end)],
                        not_do_reverse=not_do_reverse,
                        cached_like_ls=like_ls_cached
                        )
                else:
                    if not_do_reverse:
                        logger.trace("      traversal ended without next vertex.")
                        return path
                    else:
                        logger.trace("      traversal reversed without next vertex.")
                        return self.__heuristic_extend_path(
                            list(self.graph.reverse_path(path)),
                            not_do_reverse=True)
            else:
                logger.debug("    path(" + str(len(path)) + "): " + str(path))
                # if there is only one candidate
                if len(candidate_ls_list) == 1 and len(candidate_ls_list[0]) == 1:
                    read_id, strand = candidate_ls_list[0][0]
                    ovl_c_num = candidates_list_overlap_c_nums[0]
                else:
                    candidates = []
                    candidates_ovl_n = []
                    weights = []
                    num_reads_used = 0
                    for go_overlap, same_ov_cdd in enumerate(candidate_ls_list):
                        # flatten the candidate_ls_list:
                        # each item in candidate_ls_list is a list of candidates of the same overlap contig
                        candidates.extend(same_ov_cdd)
                        # record the overlap contig numbers in the flattened single-dimension candidates
                        ovl_c_num = candidates_list_overlap_c_nums[go_overlap]
                        candidates_ovl_n.extend([ovl_c_num] * len(same_ov_cdd))
                        # generate the weights for the single-dimension candidates
                        same_ov_w = [self.__read_paths_counter[self.read_paths[read_id]]
                                     for read_id, strand in same_ov_cdd]
                        num_reads_used += sum(same_ov_w)
                        same_ov_w = harmony_weights(same_ov_w, diff=self.__differ_f)
                        # RuntimeWarning: overflow encountered in exp of numpy, or math range error in exp of math
                        # change to dtype=np.float128?
                        same_ov_w = exp(np.array(log(same_ov_w) + log(self.__decay_f) * ovl_c_num, dtype=np.float128))
                        weights.extend(same_ov_w)
                        # To reduce computational burden, only reads that overlap most with current path
                        # will be considered in the extension. Proportions below 1/decay_t which will be neglected
                        # in the path proposal.
                        if num_reads_used >= self.__decay_t:
                            break
                    logger.trace("       Drawing candidates from {} reads, with [{},{}] overlaps".format(
                        num_reads_used, min(candidates_ovl_n), max(candidates_ovl_n)))
                    if not self.hetero_chromosome:
                        ######
                        # randomly chose a certain number of candidates to reduce computational burden
                        # then, re-weighting candidates by the likelihood change of adding the extension
                        ######
                        pool_size = 10  # arbitrary pool size for re-weighting
                        pool_ids = self.__random.choices(range(len(candidates)), weights=weights, k=pool_size)
                        pool_ids_set = set(pool_ids)
                        if len(pool_ids_set) == 1:
                            remaining_id = pool_ids_set.pop()
                            candidates = [candidates[remaining_id]]
                            candidates_ovl_n = [candidates_ovl_n[remaining_id]]
                            weights = [1.]
                        else:
                            # count the previous sampling and convert it into a new weights
                            new_candidates = []
                            new_candidates_ovl_n = []
                            new_weights = []
                            for remaining_id in sorted(pool_ids_set):
                                new_candidates.append(candidates[remaining_id])
                                new_candidates_ovl_n.append(candidates_ovl_n[remaining_id])
                                new_weights.append(pool_ids.count(remaining_id))
                            candidates = new_candidates
                            candidates_ovl_n = new_candidates_ovl_n
                            weights = new_weights
                            # re-weighting candidates by the likelihood change of adding the extension
                            current_vs = [v_n for v_n, v_e in path]
                            old_cov_mean, old_cov_std = self.__get_cov_mean(path, return_std=True)
                            logger.trace("      path({}): {}".format(len(path), path))
                            logger.trace("      path mean:" + str(old_cov_mean) + "," + str(old_cov_std))
                            single_cov_mean, single_cov_std = self.__get_cov_mean_of_single(path, return_std=True)
                            logger.trace("      path single mean:" + str(single_cov_mean) + "," + str(single_cov_std))
                            for go_c, (read_id, strand) in enumerate(candidates):
                                read_path = self.read_paths[read_id]
                                logger.trace("      candidate r-path {}: {}: {}".format(go_c, strand, read_path))
                                if not strand:
                                    read_path = list(self.graph.reverse_path(read_path))
                                cdd_extend = read_path[candidates_ovl_n[go_c]:]
                                logger.trace("      candidate ext {}: {}".format(go_c, cdd_extend))
                                current_v_counts = {_v_n: current_vs.count(_v_n) for _v_n, _v_e in cdd_extend}
                                like_ls = self.__cal_multiplicity_like(path=deepcopy(path),
                                                                       proposed_extension=cdd_extend,
                                                                       current_v_counts=current_v_counts,
                                                                       old_cov_mean=old_cov_mean,
                                                                       old_cov_std=old_cov_std,
                                                                       single_cov_mean=single_cov_mean,
                                                                       single_cov_std=single_cov_std)
                                like_ls_cached.append(like_ls)
                                weights[go_c] *= max(like_ls)
                            logger.trace("      like_ls_cached: {}".format(like_ls_cached))
                    elif self.__cov_inert:
                        # coverage inertia (multi-chromosomes) and not hetero_chromosome are mutually exclusive
                        # coverage inertia, more likely to extend to contigs with similar depths,
                        # which are more likely to be the same target chromosome / organelle type
                        # logger.debug(candidates)
                        # logger.debug(candidates_ovl_n)
                        cdd_cov = [self.__get_cov_mean(self.read_paths[r_id][candidates_ovl_n[go_c]:])
                                   for go_c, (r_id, r_strand) in enumerate(candidates)]
                        weights = exp(np.array([log(weights[go_c])-abs(log(cov / current_ave_coverage))
                                                for go_c, cov in enumerate(cdd_cov)], dtype=np.float128))
                    chosen_cdd_id = self.__random.choices(range(len(candidates)), weights=weights)[0]
                    if like_ls_cached:
                        like_ls_cached = like_ls_cached[chosen_cdd_id]
                    else:
                        like_ls_cached = None
                    read_id, strand = candidates[chosen_cdd_id]
                    ovl_c_num = candidates_ovl_n[chosen_cdd_id]
                read_path = self.read_paths[read_id]
                logger.trace("      read_path({}-{})({}): {}".format(ovl_c_num, len(read_path), strand, read_path))
                if not strand:
                    read_path = list(self.graph.reverse_path(read_path))
                new_extend = read_path[ovl_c_num:]
                # if not strand:
                #     new_extend = list(self.graph.reverse_path(new_extend))

                logger.debug("      potential extend(" + str(len(new_extend)) + "): " + str(new_extend))
                # input("pause")
                # logger.debug("closed_from_start: " + str(closed_from_start))
                # logger.debug("    candidate path: {} .. {} .. {}".format(path[:3], len(path), path[-3:]))
                # logger.debug("    extend path   : {}".format(new_extend))

                # if self.hetero_chromosome or self.graph.is_fully_covered_by(path + new_extend):
                return self.__heuristic_check_multiplicity(
                    # initial_mean=initial_mean,
                    # initial_std=initial_std,
                    path=path,
                    proposed_extension=new_extend,
                    not_do_reverse=not_do_reverse,
                    cached_like_ls=like_ls_cached)
                # else:
                #     return self.__heuristic_extend_path(
                #         path + new_extend,
                #         not_do_reverse=not_do_reverse,
                #         initial_mean=initial_mean,
                #         initial_std=initial_std)

    def __cal_multiplicity_like(
            self,
            path,
            proposed_extension,
            current_v_counts=None,
            old_cov_mean=None,
            old_cov_std=None,
            single_cov_mean=None,
            single_cov_std=None,
            logarithm=False,
    ):
        """
        called by __heuristic_extend_path through directly or through self.__heuristic_check_multiplicity
        :param path:
        :param proposed_extension:
        :param current_v_counts: passed to avoid repeated calculation
        :param old_cov_mean: passed to avoid repeated calculation
        :param old_cov_std: passed to avoid repeated calculation
        :param single_cov_mean: passed to avoid repeated calculation
        :param single_cov_std: passed to avoid repeated calculation
        :return: log_like_ratio_list
        """
        if not current_v_counts:
            current_vs = [v_n for v_n, v_e in path]
            current_v_counts = {v_n: current_vs.count(v_n)
                                for v_n in set([v_n for v_n, v_e in proposed_extension])}
        if not (old_cov_mean and old_cov_std):
            old_cov_mean, old_cov_std = self.__get_cov_mean(path, return_std=True)
            logger.debug("        path mean:" + str(old_cov_mean) + "," + str(old_cov_std))
            # logger.debug("initial mean: " + str(initial_mean) + "," + str(initial_std))
        # logger.trace("    old_path: {}".format(self.graph.repr_path(path)))
        # logger.trace("    checking proposed_extension: {}".format(self.graph.repr_path(proposed_extension)))
        # use single-copy mean and std instead of initial
        if not (single_cov_mean and single_cov_std):
            single_cov_mean, single_cov_std = self.__get_cov_mean_of_single(path, return_std=True)
            logger.debug("        path single mean:" + str(single_cov_mean) + "," + str(single_cov_std))

        logger.trace("        current_v_counts:{}".format(current_v_counts))
        # check the multiplicity of every vertices
        # check the likelihood of making longest extension first, than shorten the extension
        log_like_ratio_list = []
        log_like_ratio = 0.
        proposed_lengths = {_v_: self.graph.vertex_info[_v_].len for _v_, _e_ in proposed_extension}
        _old_like_cache = {}  # avoid duplicate calculation
        for v_name, v_end in proposed_extension:
            current_c = current_v_counts[v_name]
            if v_name in _old_like_cache:
                old_like = _old_like_cache[v_name]
            else:
                if current_c:
                    old_single_cov = self.contig_coverages[v_name] / float(current_c)
                    logger.trace("        old_single_cov: {}".format(old_single_cov))
                    # old_like = norm.logpdf(old_single_cov, loc=old_cov_mean, scale=old_cov_std)
                    # old_like += norm.logpdf(old_single_cov, loc=single_cov_mean, scale=single_cov_std)
                    old_like = norm.logpdf(old_single_cov, loc=single_cov_mean, scale=single_cov_std)
                else:
                    old_like = -inf
                _old_like_cache[v_name] = old_like
            new_cov_mean, new_cov_std = self.__get_cov_mean(path + [(v_name, v_end)], return_std=True)
            if current_c:
                new_single_cov = self.contig_coverages[v_name] / float(current_c + 1)
                logger.trace("        new_single_cov: {}".format(new_single_cov))
                # new_like = norm.logpdf(new_single_cov, loc=new_cov_mean, scale=new_cov_std)
                # new_like += norm.logpdf(new_single_cov, loc=single_cov_mean, scale=single_cov_std)
                new_like = norm.logpdf(new_single_cov, loc=single_cov_mean, scale=single_cov_std)
            else:
                new_like = 0.
            old_cov_mean, old_cov_std = new_cov_mean, new_cov_std
            # weighted by log(length), de-weight later for comparison
            logger.trace("        unweighted loglike ratio: {}".format(new_like - old_like))
            logger.trace("        weighting by length: {}".format(proposed_lengths[v_name]))
            logger.trace("        weighted loglike ratio: {}".format((new_like - old_like) * proposed_lengths[v_name]))
            log_like_ratio += (new_like - old_like) * proposed_lengths[v_name]
            # probability higher than 1.0 (log_ratio > 0.) will remain as 1.0 (log_ratio as 0.)
            log_like_ratio = min(0, log_like_ratio)
            log_like_ratio_list.append(log_like_ratio)
            # logger.trace("      initial_mean: %.4f, old_mean: %.4f (%.4f), proposed_mean: %.4f (%.4f)" % (
            #     initial_mean, old_cov_mean, old_cov_std, new_cov_mean, new_cov_std))
            # logger.trace("      old_like: {},     proposed_like: {}".format(old_like, new_like))

            # updating path so that new_cov_mean, new_cov_std will be updated
            path.append((v_name, v_end))
        # de-weight the log likes for comparison
        longest_ex_len = len(proposed_extension)
        v_lengths = [proposed_lengths[_v_] for _v_, _e_ in proposed_extension]
        accumulated_v_lengths = []
        for rev_go in range(len(log_like_ratio_list)):
            accumulated_v_lengths.insert(0, sum(v_lengths[:longest_ex_len - rev_go]))
        # logger.trace("    proposed_lengths: {}".format(proposed_lengths))
        # logger.trace("    accumulated_v_lengths: {}".format(accumulated_v_lengths))
        log_like_ratio_list = [_llr / accumulated_v_lengths[_go] for _go, _llr in enumerate(log_like_ratio_list)]
        if logarithm:
            return np.array(log_like_ratio_list, dtype=np.float128)
        else:
            return exp(np.array(log_like_ratio_list, dtype=np.float128))

    def __heuristic_check_multiplicity(
            self, path, proposed_extension, not_do_reverse, current_v_counts=None, cached_like_ls=None):
        """
        heuristically check the multiplicity and call a stop according to the vertex coverage and current counts
        normal distribution
        :param path:
        :param proposed_extension:
        :param not_do_reverse: True if the reverse direction has already been traversed, so do not reverse.
        :param current_v_counts: Dict
        :param cached_like_ls: input cached likelihood ratio list instead of recalculating it
        :return:
        """
        assert len(proposed_extension)
        # if there is a vertex of proposed_extension that was not used in current path,
        # accept the proposed_extension without further calculation
        if not current_v_counts:
            current_vs = [v_n for v_n, v_e in path]
            current_v_counts = {_v_n: current_vs.count(_v_n) for _v_n, _v_e in proposed_extension}
        #     current_v_counts = {}
        #     for v_name, v_end in proposed_extension:
        #         v_count = current_vs.count(v_name)
        #         if v_count:
        #             current_v_counts[v_name] = v_count
        #         else:
        #             return self.__heuristic_extend_path(
        #                 path + list(proposed_extension),
        #                 not_do_reverse=not_do_reverse)
        # else:
        #     for v_name, v_end in proposed_extension:
        #         if current_v_counts[v_name] == 0:
        #             return self.__heuristic_extend_path(
        #                 path + list(proposed_extension),
        #                 not_do_reverse=not_do_reverse)

        # extend_names = [v_n for v_n, v_e in path]
        # extend_names = {v_n: extendnames.count(v_n) for v_n in set(extend_names)}
        if not (cached_like_ls is None or cached_like_ls == []):
            like_ratio_list = cached_like_ls
        else:
            like_ratio_list = self.__cal_multiplicity_like(
                path=list(deepcopy(path)), proposed_extension=proposed_extension, current_v_counts=current_v_counts)
        # step-by-step shorten the extension
        # Given that the acceptance rate should be P_n=\prod_{i=1}^{n}{x_i} for extension with length of n,
        # where x_i is the probability of accepting contig i,
        # each intermediate random draw should follow the format below to consider the influence of accepting longer
        # extension
        #    d_{n-1} = (P_{n-1} - P_n) / (1 - P_n)
        longest_ex_len = len(proposed_extension)
        previous_like = 0.
        for rev_go, like_ratio in enumerate(like_ratio_list[::-1]):
            proposed_end = longest_ex_len - rev_go
            draw_prob = (like_ratio - previous_like) / (1. - previous_like)
            previous_like = like_ratio
            logger.trace("      draw prob:{}".format(draw_prob))
            if draw_prob > self.__random.random():
                logger.trace("      draw accepted:{}".format(proposed_extension[:proposed_end]))
                return self.__heuristic_extend_path(
                    list(deepcopy(path)) + list(proposed_extension[:proposed_end]),
                    not_do_reverse=not_do_reverse)
        else:
            # Tested to be a bad idea
            # if self.force_circular:
            #     if self.graph.is_circular_path(self.graph.roll_path(path)):
            #         logger.trace("        circular traversal ended to fit {}'s coverage.".format(
            #                      proposed_extension[0][0]))
            #         logger.trace("        checked likes: {}".format(like_ratio_list))
            #         return list(deepcopy(path))
            #     else:
            #         # do not waste current search, extend anyway
            #         return self.__heuristic_extend_path(
            #             list(deepcopy(path)) + list(proposed_extension[:1 + np.argmax(np.array(like_ratio_list))]),
            #             not_do_reverse=not_do_reverse)
            # else:
            if not_do_reverse:
                logger.trace("        linear traversal ended to fit {}'s coverage.".format(proposed_extension[0][0]))
                logger.trace("        checked likes: {}".format(like_ratio_list))
                return list(deepcopy(path))
            else:
                logger.trace("        linear traversal reversed to fit {}'s coverage.".format(proposed_extension[0][0]))
                logger.trace("        checked likes: {}".format(like_ratio_list))
                return self.__heuristic_extend_path(
                    list(self.graph.reverse_path(list(deepcopy(path)))),
                    not_do_reverse=True)

    def __index_start_subpath(self, subpath, read_id, strand):
        """
        :param subpath: tuple
        :param read_id: int, read id in self.read_paths
        :param strand: bool
        :return:
        """
        if subpath in self.__starting_subpath_to_readpaths:
            self.__starting_subpath_to_readpaths[subpath].add((read_id, strand))
        else:
            self.__starting_subpath_to_readpaths[subpath] = {(read_id, strand)}

    def __index_middle_subpath(self, subpath, read_id, strand):
        """
        :param subpath: tuple
        :param read_id: int, read id in self.read_paths
        :param strand: bool
        :param subpath_loc: int, the location of the subpath in a read
        :return:
        """
        if subpath in self.__middle_subpath_to_readpaths:
            self.__middle_subpath_to_readpaths[subpath].add((read_id, strand))
        else:
            self.__middle_subpath_to_readpaths[subpath] = {(read_id, strand)}

    def __check_path(self, path):
        assert len(path)
        try:
            for v_name, v_e in path:
                if v_name not in self.graph.vertex_info:
                    raise Exception(v_name + " not found in the assembly graph!")
        except Exception as e:
            logger.error("Invalid path: " + str(path))
            raise e

    def __get_cov_mean_of_single(self, path, return_std=False):
        """for approximate single-copy contigs"""
        self.__check_path(path)
        v_names = [v_n for v_n, v_e in path]
        v_names = {v_n: v_names.count(v_n) for v_n in set(v_names)}
        min_cov = min(v_names.values())
        v_names = [v_n for v_n, v_c in v_names.items() if v_c == min_cov]
        # use the graph structure information
        single_copy_likely = set(v_names) & self.__candidate_single_copy_vs
        if single_copy_likely:
            v_names = sorted(single_copy_likely)
        # calculate
        v_covers = []
        v_lengths = []
        for v_name in v_names:
            v_covers.append(self.contig_coverages[v_name])
            v_lengths.append(self.graph.vertex_info[v_name].len)
        mean = np.average(v_covers, weights=v_lengths)
        if return_std:
            if len(v_covers) > 1:
                std = np.average((np.array(v_covers) - mean) ** 2, weights=v_lengths) ** 0.5
                return mean, std
            else:
                return mean, mean * 0.1  # arbitrary set to be mean * 0.1
        else:
            return mean

    def __get_cov_mean(self, path, exclude_path=None, return_std=False):
        self.__check_path(path)
        v_names = [v_n for v_n, v_e in path]
        v_names = {v_n: v_names.count(v_n) for v_n in set(v_names)}
        if exclude_path:
            del_names = [v_n for v_n, v_e in exclude_path]
            del_names = {v_n: del_names.count(v_n) for v_n in set(del_names)}
            for del_n in del_names:
                if del_names[del_n] > v_names.get(del_n, 0):
                    logger.error("cannot exclude {} from {}: unequal in {}".format(exclude_path, path, del_n))
                else:
                    v_names[del_n] -= del_names[del_n]
                    if v_names[del_n] == 0:
                        del v_names[del_n]
        v_covers = []
        v_lengths = []
        for v_name in v_names:
            v_covers.append(self.contig_coverages[v_name] / float(v_names[v_name]))
            v_lengths.append(self.graph.vertex_info[v_name].len * v_names[v_name])
        # logger.trace("        > cal path: {}".format(self.graph.repr_path(path)))
        # logger.trace("        > cover values: {}".format(v_covers))
        # logger.trace("        > cover weights: {}".format(v_lengths))
        mean = np.average(v_covers, weights=v_lengths)
        if return_std:
            if len(v_covers) > 1:
                std = np.average((np.array(v_covers) - mean) ** 2, weights=v_lengths) ** 0.5
                return mean, std
            else:
                return mean, mean * 0.1  # arbitrary set to be mean * 0.1
        else:
            return mean

    # def __directed_graph_solver(
    #         self, ongoing_paths, next_connections, vertices_left, in_all_start_ve):
    #     if not vertices_left:
    #         new_paths, new_standardized = self.graph.get_standardized_isomer(ongoing_paths)
    #         if new_standardized not in self.variants_counts:
    #             self.variants_counts[new_standardized] = 1
    #             self.variants.append(new_standardized)
    #         else:
    #             self.variants_counts[new_standardized] += 1
    #         return
    #
    #     find_next = False
    #     for next_vertex, next_end in next_connections:
    #         # print("next_vertex", next_vertex, next_end)
    #         if next_vertex in vertices_left:
    #             find_next = True
    #             new_paths = deepcopy(ongoing_paths)
    #             new_left = deepcopy(vertices_left)
    #             new_paths[-1].append((next_vertex, not next_end))
    #             new_left[next_vertex] -= 1
    #             if not new_left[next_vertex]:
    #                 del new_left[next_vertex]
    #             new_connections = sorted(self.graph.vertex_info[next_vertex].connections[not next_end])
    #             if not new_left:
    #                 new_paths, new_standardized = self.graph.get_standardized_isomer(new_paths)
    #                 if new_standardized not in self.variants_counts:
    #                     self.variants_counts[new_standardized] = 1
    #                     self.variants.append(new_standardized)
    #                 else:
    #                     self.variants_counts[new_standardized] += 1
    #                 return
    #             else:
    #                 self.__directed_graph_solver(new_paths, new_connections, new_left, in_all_start_ve)
    #     if not find_next:
    #         new_all_start_ve = deepcopy(in_all_start_ve)
    #         while new_all_start_ve:
    #             new_start_vertex, new_start_end = new_all_start_ve.pop(0)
    #             if new_start_vertex in vertices_left:
    #                 new_paths = deepcopy(ongoing_paths)
    #                 new_left = deepcopy(vertices_left)
    #                 new_paths.append([(new_start_vertex, new_start_end)])
    #                 new_left[new_start_vertex] -= 1
    #                 if not new_left[new_start_vertex]:
    #                     del new_left[new_start_vertex]
    #                 new_connections = sorted(self.graph.vertex_info[new_start_vertex].connections[new_start_end])
    #                 if not new_left:
    #                     new_paths, new_standardized = self.graph.get_standardized_isomer(new_paths)
    #                     if new_standardized not in self.variants_counts:
    #                         self.variants_counts[new_standardized] = 1
    #                         self.variants.append(new_standardized)
    #                     else:
    #                         self.variants_counts[new_standardized] += 1
    #                 else:
    #                     self.__directed_graph_solver(new_paths, new_connections, new_left, new_all_start_ve)
    #                     break
    #         if not new_all_start_ve:
    #             return

    # def __circular_directed_graph_solver(self,
    #     ongoing_path,
    #     next_connections,
    #     vertices_left,
    #     check_all_kinds,
    #     palindromic_repeat_vertices,
    #     ):
    #     """
    #     recursively exhaust all circular paths, deprecated for now
    #     :param ongoing_path:
    #     :param next_connections:
    #     :param vertices_left:
    #     :param check_all_kinds:
    #     :param palindromic_repeat_vertices:
    #     :return:
    #     """
    #     if not vertices_left:
    #         new_path = deepcopy(ongoing_path)
    #         if palindromic_repeat_vertices:
    #             new_path = [(this_v, True) if this_v in palindromic_repeat_vertices else (this_v, this_e)
    #                         for this_v, this_e in new_path]
    #         if check_all_kinds:
    #             rev_path = self.graph.reverse_path(new_path)
    #             this_path_derived = [new_path, rev_path]
    #             for change_start in range(1, len(new_path)):
    #                 this_path_derived.append(new_path[change_start:] + new_path[:change_start])
    #                 this_path_derived.append(rev_path[change_start:] + rev_path[:change_start])
    #             standardized_path = tuple(sorted(this_path_derived)[0])
    #             if standardized_path not in self.variants_counts:
    #                 self.variants_counts[standardized_path] = 1
    #                 self.variants.append(standardized_path)
    #             else:
    #                 self.variants_counts[standardized_path] += 1
    #         else:
    #             new_path = tuple(new_path)
    #             if new_path not in self.variants_counts:
    #                 self.variants_counts[new_path] = 1
    #                 self.variants.append(new_path)
    #             else:
    #                 self.variants_counts[new_path] += 1
    #         return
    #
    #     for next_vertex, next_end in next_connections:
    #         # print("next_vertex", next_vertex)
    #         if next_vertex in vertices_left:
    #             new_path = deepcopy(ongoing_path)
    #             new_left = deepcopy(vertices_left)
    #             new_path.append((next_vertex, not next_end))
    #             new_left[next_vertex] -= 1
    #             if not new_left[next_vertex]:
    #                 del new_left[next_vertex]
    #             new_connections = self.graph.vertex_info[next_vertex].connections[not next_end]
    #             if not new_left:
    #                 if (self.__start_vertex, not self.__start_direction) in new_connections:
    #                     if palindromic_repeat_vertices:
    #                         new_path = [
    #                             (this_v, True) if this_v in palindromic_repeat_vertices else (this_v, this_e)
    #                             for this_v, this_e in new_path]
    #                     if check_all_kinds:
    #                         rev_path = self.graph.reverse_path(new_path)
    #                         this_path_derived = [new_path, rev_path]
    #                         for change_start in range(1, len(new_path)):
    #                             this_path_derived.append(new_path[change_start:] + new_path[:change_start])
    #                             this_path_derived.append(rev_path[change_start:] + rev_path[:change_start])
    #                         standardized_path = tuple(sorted(this_path_derived)[0])
    #                         if standardized_path not in self.variants_counts:
    #                             self.variants_counts[standardized_path] = 1
    #                             self.variants.append(standardized_path)
    #                         else:
    #                             self.variants_counts[standardized_path] += 1
    #                     else:
    #                         new_path = tuple(new_path)
    #                         if new_path not in self.variants_counts:
    #                             self.variants_counts[new_path] = 1
    #                             self.variants.append(new_path)
    #                         else:
    #                             self.variants_counts[new_path] += 1
    #                     return
    #                 else:
    #                     return
    #             else:
    #                 new_connections = sorted(new_connections)
    #                 self.__circular_directed_graph_solver(new_path, new_connections, new_left, check_all_kinds,
    #                                                       palindromic_repeat_vertices)


