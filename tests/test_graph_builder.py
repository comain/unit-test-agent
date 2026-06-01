import pytest
import os
from uta.language.java.parse.java_parser import JavaParser
from uta.language.java.parse.graph_builder import GraphBuilder

def test_build_graph(fixtures_dir):
    parser = JavaParser()
    service_path = os.path.join(fixtures_dir, "SampleService.java")
    mapper_path = os.path.join(fixtures_dir, "SampleMapper.java")
    biz_path = os.path.join(fixtures_dir, "SampleBizImpl.java")
    
    results = [
        parser.parse_file(service_path),
        parser.parse_file(mapper_path),
        parser.parse_file(biz_path)
    ]
    
    builder = GraphBuilder()
    graph = builder.build(results)
    
    # Check nodes
    assert "com.example.service.SampleService.process" in graph.nodes
    assert "com.example.mapper.SampleMapper.selectById" in graph.nodes
    assert "com.example.biz.SampleBizImpl.execute" in graph.nodes
    
    # Check edges
    edges = {(e.source, e.target, e.relation) for e in graph.edges}
    
    # SampleService.process -> SampleMapper.selectById
    assert ("com.example.service.SampleService.process", 
            "com.example.mapper.SampleMapper.selectById", "CALLS") in edges
            
    # SampleBizImpl.execute -> SampleService.process
    assert ("com.example.biz.SampleBizImpl.execute", 
            "com.example.service.SampleService.process", "CALLS") in edges

    # CONTAINS edges
    assert ("com.example.service.SampleService", 
            "com.example.service.SampleService.process", "CONTAINS") in edges
