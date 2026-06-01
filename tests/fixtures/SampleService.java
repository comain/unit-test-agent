package com.example.service;

import javax.annotation.Resource;
import org.springframework.stereotype.Service;
import com.example.mapper.SampleMapper;

@Service
public class SampleService {

    @Resource
    private SampleMapper sampleMapper;

    public String process(String input) {
        if (input == null) {
            return "empty";
        }
        return sampleMapper.selectById(input);
    }
}
