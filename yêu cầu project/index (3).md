---
title: "Coding Session"
short_title: "Coding Session"
label: intro-ds-session
description: "Individual coding session statement of the Introduction to Data Science course. One runnable notebook moves the designated data through acquisition, transformation, quality assessment, exploration, modeling, evaluation, and deployment."
---

# Objectives

This coding session exercises the complete arc of the course end to end. Each participant works on the data designated for this statement and walks it through the full pipeline, from data collection and data management through data quality assessment and exploratory data analysis to modeling, evaluation, and deployment. At the end of the work you should be able to:

- Acquire the designated data source faithfully.
- Transform the acquired records into a working tabular dataset, with every derived column documented in the code.
- Assess the data quality of the resulting table on several relevant aspects discussed in the lectures.
- Explore the table for and justify at least two significant insights about the underlying data.
- Train, evaluate, and compare learning pipelines, and serve the winning pipeline behind a small interface.

You work independently and manage your own progress within the announced duration. The details of the data, together with the sources to practice on, sit in the closing section.

# Session Rules

- This is an **individual assignment**, and all work is your own. When the work is taken under supervision, the sitting runs for the duration announced for it.
- Because this work is done without AI assistance, all AI tools and code copilots are strictly prohibited while you work on it. A second warning deducts half of the score, and a third warning deducts the entire score.
- Plagiarism, dishonesty, or any other misconduct means a grade of zero for the course.

All explanations, reasoning, and conclusions belong in the notebook you deliver, and the notebook falls under the same rules as every other deliverable.

**Good luck!**

# Task 1: Data Acquisition

Acquire the data from the source you are given, and keep its original form intact:

1. Acquire the records the source offers. When the source exposes an API, drive it programmatically: construct the right request for every needed record, request the fields you plan to use in the working table, respect the rate limits with a suitable delay between requests, and give a failing request a capped number of retries before you log and skip the entries that keep failing.
2. Choose whether to persist the acquired records or carry them straight into the working table. When you persist them, keep them faithful to what the source returned, without flattening or dropping fields, and use a store that suits the source, for example a document store such as MongoDB Atlas or a directory of files.
3. Store the identifying keys of each record explicitly wherever the records live, so any record can be picked out and re-queried later.
4. Verify the counts at each step: the acquired records against the source and, where you persisted them, the store against the acquired records. Keep a log of the successes plus the failures with short error messages.

Whether you persist the records or transform them directly, the path from acquisition to the finished table runs as one automated pass.

# Task 2: Tabular Transformation and Data Quality

## Building the working table

Starting from the acquired records, staged or not, write code that:

1. Takes every acquired record that belongs to the assignment, whether it comes from your store or straight from the acquisition.
2. Derives the working columns from the fields the records carry, choosing the schema yourself unless the instructors specify one, and covering the attributes the later tasks need.
3. Directs the result into one or more CSV files with the columns you chose, unless the instructors specify the files or the schema.

Document in the code how each column is derived from the raw records. Handle empty values, missing fields, and partial responses deliberately rather than silently, because the quality of this table sets the ceiling for everything after it.

## Assessing data quality

With the table in place, assess it:

- Open the assessment with your own words for the notion of data quality, as the lectures introduced it.
- Choose several relevant quality aspects, covering clearly more than one or two, with candidates such as correctness, completeness, consistency, timeliness, and interpretability.
- Operationalize every chosen aspect in this setting by stating how the columns of the table measure or expose the problem.
- Support the assessment with quantitative summaries or simple visualizations, for example counts of missing values, distributions of suspicious entries, and the proportion of rows that each issue touches.
- Conclude with the main issues found, their likely impact on the later tasks, and the mitigation strategies you would recommend.

The aim of this assessment is not to apply every concept from the lectures but to show that you can select, justify, and apply several suitable notions to the concrete dataset at hand.

# Task 3: Exploratory Data Analysis

With the working table in hand, uncover and articulate valuable patterns inside the data. Drive the exploration with guiding questions of your own, formulated from what the dataset suggests, for example questions around the variables of interest, around single variables, or around relationships between variables.

For each question, pick the techniques that suit the variables involved. Match numerical summaries and plots to the nature of the variables, whether they are categorical or numerical, and to the relationship under examination, whether one variable alone or several together. Implement them in the notebook.

Give every main question and every major pattern a short written interpretation that:

- says what the summary or the plot shows in plain language,
- interprets the pattern as reasonable, surprising, or useful in the context of the dataset,
- acknowledges the limitations the dataset carries, from the sample size through the data quality issues already found.

The goal is to arrive at convincing, well-argued insights, and the notebook carries at least two significant insights for this task, unless the instructors specify otherwise.

# Task 4: Modeling and Evaluation

## Modeling

Prepare the working table for a supervised learning task, choosing classification or regression from the nature of the target column. Where the instructors specify the problem, the models, or the metrics, follow that guidance. Otherwise choose what best suits the data and justify each choice in the notebook.

- Split the table into training and testing sets at an 80/20 ratio, keep the two sides' distributions close through stratification on the target column where class labels apply, and fix `random_state=42`.
- Preprocess strictly inside a `Pipeline`. Encode the categorical features, scale the numerical ones where it helps the model, and wire these steps through a `ColumnTransformer`. Manual preprocessing outside the pipeline is not allowed.
- Train at least two distinct models suited to the chosen task, each implemented as an end-to-end pipeline from preprocessing through learning. For a classification task, logistic regression, decision tree, random forest, nearest neighbors, and naive Bayes stand as well-tested starting points. For a regression task, linear regression, ridge, tree ensembles, and nearest-neighbor regressors stand in.
- Keep, for each trained model, the test-set predictions in the form the evaluation needs: the predicted class labels and the predicted class probabilities where the task is a classification, and the raw predictions elsewhere.

## Evaluation

Score every trained model on the prepared outputs. Common choices include:

| Metric block | What to report |
|---|---|
| Performance overview | The headline measure the task offers, accuracy for a classification or the error summary for other tasks, together with the confusion matrix rendered as a heatmap where class labels exist. |
| Task report | Macro precision, macro recall, and macro F1-score for a classification, or error measures such as MAE and RMSE together with the coefficient of determination for a regression. |
| Discrimination | The macro-averaged ROC-AUC computed from the predicted class probabilities, where the task is a classification. |

Once all models carry their scores, compare them across the metrics, analyze where they lose through the overview views, discuss the plausible reasons behind the losses, for example class imbalance, classes that overlap, or outlier-heavy regions, and recommend the model or models to keep, supported by the metrics and by the shape of the dataset.

# Task 5: Serving the Winning Model

- Take the winning pipeline from Task 4, persist it, and load it back, so the served artifact is the end-to-end pipeline rather than a bare estimator.
- Wrap it in a Gradio interface that takes the feature inputs of the dataset and returns the predicted output for what the user enters.
- Unless the instructors specify the serving route, choose the route that fits the environment. Launch the interface directly from the notebook or publish it as a Hugging Face Space through the Gradio SDK. Record its reachable link in the notebook.

# Submission

Everything the assignment grades lives in one Google Colab notebook:

- the code for the five tasks, runnable from the first cell through the last,
- the interpretations and conclusions written as markdown cells throughout,
- the CSV files of Task 2 exported inside the notebook,
- the serving route in play realized in the notebook, with the reachable link recorded, including the published Hugging Face Space when the interface is hosted there.

Running the notebook from top to bottom reproduces every result the assignment grades, down to the serving route in play. Re-run it that way before submission, so that every cell carries its executed output and the delivered link opens straight into a working state. Submit a single link to the runnable notebook through Moodle.

# Assessment

| No. | Details | Score |
|---|---|---|
| 1 | Data acquisition | 20% |
| 2 | Tabular transformation and data quality | 20% |
| 3 | Exploratory data analysis, at least two significant insights | 25% |
| 4 | Learning pipelines, evaluation, and comparison | 25% |
| 5 | Serving the winning model | 10% |

# The Data

The data for this assignment comes from the instructors, and what it contains and how it is delivered can vary. Read the data as it is provided.

## Practice sources

Practice the pipeline any time against data you can reach freely. The sources below are public, free, and each fits a block of the statement:

| Source | Kind | Address | Practice fit |
|---|---|---|---|
| OpenAlex | Batch | https://openalex.org | JSON records over HTTP for Tasks 1 through 3 |
| World Bank data API | Batch | https://data.worldbank.org | JSON records over HTTP for Tasks 1 through 3 |
| UCI Machine Learning Repository | Batch | https://archive.ics.uci.edu | Tabular sets with a target column for Tasks 2 through 4 |
| OpenML | Batch | https://www.openml.org | Tabular sets with a target column for Tasks 2 through 4 |
| Kaggle datasets | Batch | https://www.kaggle.com/datasets | Tabular sets with a target column for Tasks 2 through 4 |
| Hugging Face datasets | Batch | https://huggingface.co/datasets | Tabular sets with a target column for Tasks 2 through 4 |
| Open-Meteo | Live | https://open-meteo.com | Refreshing weather records with rate limits to respect, snapshots feed Tasks 2 through 4 |
| Frankfurter | Live | https://frankfurter.app | Refreshing exchange rates, snapshots feed Tasks 2 through 4 |
| CoinGecko | Live | https://docs.coingecko.com | Refreshing crypto market data, snapshots feed Tasks 2 through 4 |

A batch dataset or a snapshot of a live feed is enough for a full dry run: collect it, derive the table, check its quality, explore it, train several pipelines, and serve one interface. What you practice on stays your choice.
