#!/usr/bin/env python.
import math
import os

import numpy as np
import pandas as pd
import torch
from dotenv import load_dotenv
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, confusion_matrix
from transformers import DistilBertTokenizerFast, DistilBertForSequenceClassification, Trainer, TrainingArguments

from machine_info_identifier import remove_log, remove_code_block, remove_url, remove_markdown, has_log, has_code_block, \
    has_url

load_dotenv()
ROOT = os.environ.get("ROOT")

options = {
    "preprocess": [remove_markdown, remove_code_block],  # remove_markdown, remove_code_block, remove_url, remove_log
    "features": ["title", "body"],  # title, body
    "load_train_path": f"{ROOT}/pipeline/pickles/dataframe_train.pkl",
    "load_test_path": f"{ROOT}/pipeline/pickles/dataframe_test.pkl",
    "save_dir": f"{ROOT}/results/code",
    # load_dir": f"{ROOT}/results/title-body",  # If None, will train from scratch,
    "load_dir": None,
    "test_mode": False,
    "device": torch.device("cuda"),  # cpu, cuda
    "train_test_split": 0.8,
    "num_train_epochs": 3,
    "per_device_train_batch_size": 16,
    "per_device_eval_batch_size": 64,
    "warmup_steps": 500,
    "weight_decay": 0.01,
    "SEED": 1,
    "logging_steps": 10
}

# Consts
if options["load_dir"]:
    MODEL = DistilBertForSequenceClassification.from_pretrained(options["load_dir"], num_labels=3)
    TOKENIZER = DistilBertTokenizerFast.from_pretrained(options["load_dir"])
else:
    MODEL = DistilBertForSequenceClassification.from_pretrained("distilbert-base-uncased", num_labels=3)
    TOKENIZER = DistilBertTokenizerFast.from_pretrained('distilbert-base-uncased')


class Dataset(torch.utils.data.Dataset):
    def __init__(self, encodings, labels):
        self.encodings = encodings
        self.labels = labels

    def __getitem__(self, idx):
        item = {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}
        item['labels'] = torch.tensor(self.labels[idx])
        return item

    def __len__(self):
        return len(self.labels)


def preprocess(df):
    for fn in options["preprocess"]:
        df.apply(fn)
    return df


def load_dataframe_from_pickle(path):
    retrieved_df = pd.read_pickle(path)
    return retrieved_df


def pretty_dict(dict):
    """ Returns a pretty string version of a dictionary.
    """
    result = ""
    for key, value in dict.items():
        key = str(key)
        value = str(value)
        if len(value) < 40:
            result += f'{key}: {value} \n'
        else:
            result += f'{key}: \n' \
                      f'{value} \n'
    return result


def prep_single_dataset(df):
    encodings = TOKENIZER(df["X"].tolist(), truncation=True, padding=True)
    dataset = Dataset(encodings, df["labels"].tolist())
    return dataset


def prep_datasets(df):
    """ Returns all, code, url, log dataset from list of training data (str) and list of labels (int). """
    all_ds = prep_single_dataset(df)

    result = [all_ds]
    fns = [has_code_block, has_url, has_log]
    print(f"Shape before filtering: {df.shape}")
    for fn in fns:
        df_ = df[df["X"].apply(fn)]
        print(f"Shape after {fn}: {df_.shape}")
        assert df_.size <= df.size, "filtered df should not be of bigger size than original one"
        ds = prep_single_dataset(df_)
        result.append(ds)

    return result


def compute_metrics(pred):
    labels = pred.label_ids
    preds = pred.predictions.argmax(-1)
    acc = accuracy_score(labels, preds)
    precision, recall, fscore, _ = precision_recall_fscore_support(labels, preds, average="weighted")  # weighted to account for label imbalance
    cm = confusion_matrix(labels, preds)
    return {
        'accuracy': acc,
        'precision': precision,
        'recall': recall,
        'fscore': fscore,
        'cm': cm,  # tensorboard may drop this but that's fine - tensorboard's a sep logging framework
    }


def train_model(train_dataset):
    print("Training model...")
    no_cuda = options["device"] == torch.device('cpu')
    training_args = TrainingArguments(
        output_dir=options["save_dir"],  # output directory
        num_train_epochs=options["num_train_epochs"],  # total number of training epochs
        per_device_train_batch_size=options["per_device_train_batch_size"],  # batch size per device during training
        per_device_eval_batch_size=options["per_device_eval_batch_size"],  # batch size for evaluation
        warmup_steps=options["warmup_steps"],  # number of warmup steps for learning rate scheduler
        weight_decay=options["weight_decay"],  # strength of weight decay
        logging_dir='./logs',  # directory for storing logs
        logging_steps=options["logging_steps"],
        no_cuda=no_cuda,
    )

    trainer = Trainer(
        model=MODEL,  # the instantiated 🤗 Transformers model to be trained
        args=training_args,  # training arguments, defined above
        train_dataset=train_dataset,  # training dataset
        compute_metrics=compute_metrics  # accuracy metric
    )

    trainer.train()

    return trainer


def load_model():
    print("Loading model...")
    no_cuda = options["device"] == torch.device('cpu')
    training_args = TrainingArguments(  # no trg is done
        output_dir=options["save_dir"],
        no_cuda=no_cuda,
        logging_dir='./logs',  # directory for storing logs
    )

    trainer = Trainer(
        model=MODEL,  # the instantiated 🤗 Transformers model to be trained
        args=training_args,
        compute_metrics=compute_metrics  # accuracy metric
    )

    return trainer


def main():
    print("Preparing data...")
    # Setting seeds to control randomness
    np.random.seed(options["SEED"])
    torch.manual_seed(options["SEED"])

    # Load data
    train_data = load_dataframe_from_pickle(options["load_train_path"])
    test_data = load_dataframe_from_pickle(options["load_test_path"])

    # Retrieve features
    print("Retrieving features...")
    train_data['X'] = ''
    test_data['X'] = ''
    for feature in options["features"]:
        train_data['X'] += train_data[feature] + " "
        test_data['X'] += test_data[feature] + " "

    # Preprocess
    print("Preprocessing...")
    train_data['X'] = preprocess(train_data['X'])
    test_data['X'] = preprocess(test_data['X'])

    # Preparing model
    print("Preparing model...")
    # [NOTE: No need to randomise as randomisation has already been done in scripts/dataframe_generator.py]
    if options["test_mode"]:
        test_lite_dataset = prep_datasets(test_data[:10])  # for dev testing
        assert len(test_lite_dataset) == 4
    else:
        training_length = math.ceil(len(train_data.index) * options["train_test_split"])
        if not bool(options["load_dir"]): train_dataset = prep_single_dataset(train_data[:training_length])
        test_seen_datasets = prep_datasets(train_data[training_length:])  # all, code, url, log
        test_unseen_datasets = prep_datasets(test_data)
        assert len(test_seen_datasets) == 4
        assert len(test_unseen_datasets) == 4


    # Building model
    if bool(options["load_dir"]):
        trainer = load_model()
    else:  # train from scratch
        trainer = train_model(train_dataset)
        print("Saving the good stuff in case they get lost...")
        trainer.save_model(options["save_dir"])
        TOKENIZER.save_pretrained(options["save_dir"])

    print("Evaluating...")
    info = options
    ds_type = ["all", "code", "url", "log"]
    if options["test_mode"]:
        for idx, ds in enumerate(test_lite_dataset):
            result = trainer.evaluate(ds)
            result["dataset size"] = len(ds)
            info[f"results on {ds_type[idx]} unseen lite repos"] = result
    else:
        all_test_ds = [test_seen_datasets, test_unseen_datasets]
        all_test_ds_type = ["seen", "unseen"]
        for i, test_ds in enumerate(all_test_ds):
            for j, ds in enumerate(test_ds):
                result = trainer.evaluate(ds)
                result["dataset size"] = len(ds)
                info[f"results on {all_test_ds_type[i]} {ds_type[j]} repos"] = result

    # saving results and model
    print("Saving all the good stuff...")
    data_file = open(f'{options["save_dir"]}/data.txt', "w+")
    data_file.write(pretty_dict(info))
    data_file.close()


if __name__ == "__main__":
    main()
