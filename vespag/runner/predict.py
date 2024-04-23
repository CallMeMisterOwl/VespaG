import csv
from pathlib import Path
from typing import Optional

import h5py
import numpy as np
import torch
import typer
from Bio import SeqIO
from tqdm import tqdm
from typing_extensions import Annotated

from vespag.utils.type_hinting import *
from vespag.utils import AMINO_ACIDS, compute_mutation_score, DEFAULT_MODEL_PARAMETERS, get_device, load_model, mask_non_mutations, read_mutation_file, SAV, setup_logger


def predict(
    fasta_file: Annotated[Path, typer.Option("-i", "--input", help="Path to FASTA-formatted file containing protein sequence(s)")],
    output_path: Annotated[Path, typer.Option("-o", "--output", help="Path for saving created CSV and/or H5 files. Defaults to ./output")] = None,
    embedding_file: Annotated[Path, typer.Option("-e", "--embeddings", help="Path to pre-generated input embeddings. Embeddings will be generated from scratch if no path is provided.")] = None,
    mutation_file: Annotated[Path, typer.Option("--mutation-file", help="CSV file specifying specific mutations to score")] = None,
    id_map_file: Annotated[Path, typer.Option("--id-map", help="CSV file mapping embedding IDs to FASTA IDs if they're different")] = None,
    single_csv: Annotated[Optional[bool], typer.Option("--single-csv/--multi-csv", help="Whether to return one CSV file for all proteins instead of a single file for each protein")] = False,
    no_csv: Annotated[bool, typer.Option("--no-csv/--csv", help="Whether no CSV output should be produced at all")] = False,
    h5_output: Annotated[bool, typer.Option("--h5-output/--no-h5-output", help="Whether a file containing predictions in HDF5 format should be created")] = False,
    zero_based_mutations: Annotated[bool, typer.Option("--zero-idx/--one-idx", help="Whether to enumerate the sequence starting at 0.")] = False,
) -> None:
    logger = setup_logger()

    output_path = output_path or Path.cwd() / "output"
    if not output_path.exists():
        logger.info(f"Creating output directory {output_path}")
        output_path.mkdir(parents=True)
    
    device = get_device()
    model = load_model(**DEFAULT_MODEL_PARAMETERS).eval().to(device, dtype=torch.float)

    sequences = {rec.id: str(rec.seq) for rec in SeqIO.parse(fasta_file, "fasta")}

    if embedding_file:
        logger.info(f"Loading pre-computed embeddings from {embedding_file}")
        embeddings = {id: torch.from_numpy(np.array(emb[()], dtype=np.float32)).to(device) for id, emb in
                    tqdm(h5py.File(embedding_file).items(), desc="Loading embeddings", leave=False)}
        if id_map_file:
            id_map = {row[0]: row[1] for row in csv.reader(id_map_file.open('r'))}
            for from_id, to_id in id_map_file.items():
                embeddings[to_id] = embeddings[from_id]
                del embeddings[from_id]

    else:
        logger.info("Generating ESM2 embeddings")
        if "HF_HOME" in os.environ:
            plm_cache_dir = os.environ["HF_HOME"]
        else:
            plm_cache_dir = Path.cwd() / ".esm2_cache"
            plm_cache_dir.mkdir(exist_ok=True)
        embedder = Embedder("facebook/esm2_t36_3B_UR50D", plm_cache_dir)
        embeddings = embedder.embed(sequences)
        embedding_output_path = output_path / "esm2_embeddings.h5"
        logger.info(f"Saving generated ESM2 embeddings to {embedding_output_path} for re-use")
        Embedder.save_embeddings(embeddings, embedding_output_path)
    
    logger.info("Generating mutational landscape")
    if mutation_file:
        mutations_per_protein = read_mutation_file(mutation_file, one_indexed=not zero_based_mutations)
    else:
        mutations_per_protein = {
            protein_id: [
                SAV(i, wildtype_aa, other_aa, not zero_based_mutations)
                for i, wildtype_aa in enumerate(sequence)
                for other_aa in AMINO_ACIDS if other_aa != wildtype_aa]
            for protein_id, sequence in tqdm(sequences.items(), leave=False)}

    logger.info("Generating predictions")
    vespag_scores = {}
    scores_per_protein = {}
    pad_length = max([len(id) for id in embeddings.keys()])
    for id, sequence in (pbar := tqdm(sequences.items(), leave=False)):
        pbar.set_description(f"Current protein: {id}".ljust(pad_length + 20))
        embedding = embeddings[id]
        y = model(embedding)
        y = mask_non_mutations(y, sequence)
            
        scores_per_protein[id] = {
            mutation: compute_mutation_score(y, mutation)
            for mutation in mutations_per_protein[id]
        }
        if h5_output:
            vespag_scores[id] = y.detach().numpy()

    if h5_output:
        h5_output_path = output_path / "vespag_scores_all.h5"
        logger.info(f"Serializing predictions to {h5_output_path}")
        with h5py.File(h5_output_path, 'w') as f:
            for id, vespag_prediction in tqdm(vespag_scores.items(), leave=False):
                f.create_dataset(id, data=vespag_prediction)

    if not no_csv:
        logger.info("Generating CSV output")
        if not single_csv:
            for protein_id, mutations in tqdm(scores_per_protein.items(), leave=False):
                output_file = output_path / (protein_id + ".csv")
                with output_file.open("w+") as f:
                    f.write("Mutation, VespaG\n")
                    f.writelines([f"{str(sav)},{score}\n" for sav, score in mutations.items()])
        else:
            output_file = output_path / "vespag_scores_all.csv"
            with output_file.open("w+") as f:
                f.write("Mutation, VespaG\n")
                f.writelines([line for line in tqdm([
                    f"{protein_id}_{str(sav)},{score}\n"
                    for protein_id, mutations in scores_per_protein.items()
                    for sav, score in mutations.items()], leave=False)
                              ])


if __name__ == "__main__":
    typer.run(predict)
