package com.example.biz;

import javax.annotation.Resource;
import org.springframework.stereotype.Service;
import com.example.service.SampleService;

@Service
public class SampleBizImpl {

    @Resource
    private SampleService sampleService;

    public void execute(String id) {
        String result = sampleService.process(id);
        System.out.println("Result: " + result);
    }
}
