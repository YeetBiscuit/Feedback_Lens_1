# Project Overview

This project implements a Retrieval-Augmented Generation (RAG) system for generating personalised feedback on computing assignments in higher education.

The system is designed to assist educators (e.g., tutors) by providing structured, rubric-aligned feedback on student submissions.

---

# System Context

The system operates on three types of inputs:

1. Course-level materials (for retrieval)
   - Lecture slides
   - Lecture notes
   - Tutorial materials
   - Sample answers

2. Assignment-level documents
   - Assignment specification
   - Marking rubric

3. Submission-level input
   - Student submission

These inputs are used to generate structured feedback grounded in course materials and aligned with the rubric.

---

# Baseline Version (Current Scope)

This project is currently implementing the **baseline version** of the system.

The baseline uses a **direct retrieval approach**:
- Retrieval queries are constructed directly from:
  - assignment specification
  - marking rubric
  - student submission

---

# Pipeline (Baseline)

1. Course materials are:
   - chunked
   - embedded
   - stored in a vector database

2. For each student submission:
   - A retrieval query is constructed directly from:
     - assignment specification
     - marking rubric
     - student submission

3. Relevant course materials are retrieved (top-k)

4. A structured prompt is built using:
   - retrieved context
   - assignment specification
   - marking rubric
   - student submission

5. The LLM generates structured feedback

---

# Output Requirements

The generated feedback MUST follow this structure:

- Criterion-level strengths
- Areas for improvement
- Suggestions
- Optional grade band (coarse-grained, not precise scoring)

---

# Design Constraints

- DO NOT implement a retrieval planner at this stage
- DO NOT introduce multi-step agent workflows
- DO NOT significantly restructure the pipeline
- Prefer minimal and modular code changes
- Keep the design extensible for future integration of a retrieval planner

---

# Future Extension (Not Implemented Yet)

A future version of the system will include an **LLM-based retrieval planner** that:
- extracts structured signals from inputs
- generates targeted retrieval cues
- improves alignment between retrieved context and assignment requirements

This functionality MUST NOT be implemented in the baseline.

---

# Coding Guidelines

- Keep functions small and readable
- Avoid unnecessary dependencies
- Separate concerns clearly:
  - data handling
  - retrieval
  - generation
- Ensure components can be easily replaced or extended

---

# Key Principle

The goal of the baseline is to establish a **simple, direct retrieval pipeline** that can serve as a reference point for evaluating more advanced retrieval strategies (e.g., retrieval planning).